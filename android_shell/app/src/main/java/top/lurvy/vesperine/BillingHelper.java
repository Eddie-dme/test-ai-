package top.lurvy.vesperine;

import android.app.Activity;
import android.content.Context;
import android.util.Log;

import com.android.billingclient.api.BillingClient;
import com.android.billingclient.api.BillingClientStateListener;
import com.android.billingclient.api.BillingFlowParams;
import com.android.billingclient.api.BillingResult;
import com.android.billingclient.api.ConsumeParams;
import com.android.billingclient.api.ProductDetails;
import com.android.billingclient.api.Purchase;
import com.android.billingclient.api.QueryProductDetailsParams;
import com.android.billingclient.api.QueryPurchasesParams;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Google Play 结算客户端。
 *
 * 职责边界（重要）
 * ----------------
 * 这个类**只做两件事**：拉起付款界面、拿到 purchaseToken。
 *
 * 它**不发放 hearts** —— hearts 必须由服务端校验 token 之后才发。
 * 原因：客户端上报「我买成功了」完全不可信，改一个请求就能白拿。
 * 服务端拿 token 去 Google 核对，这是唯一可靠的路径
 * （见 floze_clone/iap_verify.py）。
 *
 * 完整链路
 * --------
 *   1. 前端 VesperineNative.buyHearts(planId)
 *   2. 本类 launchBillingFlow → 用户在 Play 付款页完成支付
 *   3. onPurchasesUpdated 拿到 purchaseToken
 *   4. 回调前端，前端带着 token 请求服务端 /api/heart/purchase
 *   5. 服务端向 Google 核对 → 发 hearts → 返回余额
 *   6. 前端成功后调 VesperineNative.consumePurchase(token)
 *
 * 为什么 consume 放在最后
 * ----------------------
 * 消耗型商品必须先 consume 才能再次购买同一档。但如果服务端校验失败
 * 就 consume 了，用户付了钱却拿不到 hearts，而且无法重试 —— 钱白花了。
 * 所以顺序必须是：服务端发放成功 → 才 consume。
 *
 * 未 consume 的订单会留在 Play 的购买记录里，下次启动时
 * queryPurchasesAsync 能捞回来补发（走同一套服务端校验）。
 */
public class BillingHelper {

    private static final String TAG = "VesperineBilling";

    /** 商品 ID —— 必须与 Play Console 里创建的应用内商品完全一致，
     *  也必须与服务端 API.IAP_PLANS 的 key 一致。三处任何一处改了都要同步。 */
    private static final String[] PLAN_IDS = {
            "top.lurvy.vesperine.hearts30",
            "top.lurvy.vesperine.hearts100",
            "top.lurvy.vesperine.hearts300",
            "top.lurvy.vesperine.hearts800",
    };

    public interface Callback {
        /** 商品列表就绪。products 是 JSON 数组字符串。 */
        void onProducts(String productsJson);

        /** 拿到 purchaseToken，需要交给服务端校验。 */
        void onPurchaseToken(String planId, String purchaseToken);

        /** 出错。code 便于前端区分是「商店不可用」还是「用户取消」。 */
        void onError(String code, String message);

        /** 购买记录恢复（用于补发）。每一条都单独回调 onPurchaseToken。 */
        void onRestoreDone(int count);
    }

    private final Context context;
    private BillingClient client;
    private Callback callback;
    private boolean connecting = false;
    private int pendingRequests = 0;   // 连接就绪后要补做的事

    private final Map<String, ProductDetails> productCache = new HashMap<>();

    public BillingHelper(Context ctx) {
        this.context = ctx.getApplicationContext();
    }

    public void setCallback(Callback cb) {
        this.callback = cb;
    }

    // ── 连接管理 ─────────────────────────────────────────────

    private boolean ready() {
        return client != null && client.isReady();
    }

    /**
     * 确保已连上 Play 商店。
     * 连接是异步的，所以用 runWhenReady 把后续动作排队。
     */
    private void ensureConnected(Runnable then) {
        if (ready()) {
            then.run();
            return;
        }
        if (connecting) {
            // 已在连接中，等它完成
            pendingRequests++;
            return;
        }
        connecting = true;

        if (client == null) {
            client = BillingClient.newBuilder(context)
                    .setListener((BillingResult result, List<Purchase> purchases) ->
                            handlePurchaseUpdate(result, purchases))
                    .enablePendingPurchases()
                    .build();
        }

        client.startConnection(new BillingClientStateListener() {
            @Override
            public void onBillingSetupFinished(BillingResult result) {
                connecting = false;
                if (result.getResponseCode() == BillingClient.BillingResponseCode.OK) {
                    Log.i(TAG, "Play 结算已就绪");
                    then.run();
                    // 把排队等待的请求一次性放行
                    for (int i = 0; i < pendingRequests; i++) {
                        queryProducts();
                    }
                    pendingRequests = 0;
                } else {
                    Log.w(TAG, "连接失败 code=" + result.getResponseCode()
                            + " " + result.getDebugMessage());
                    pendingRequests = 0;
                    if (callback != null) {
                        callback.onError("NOT_AVAILABLE",
                                "Play 商店不可用（" + result.getResponseCode() + "）");
                    }
                }
            }

            @Override
            public void onBillingServiceDisconnected() {
                Log.w(TAG, "Play 结算连接断开");
                connecting = false;
                productCache.clear();
            }
        });
    }

    // ── 查询商品 ─────────────────────────────────────────────

    /**
     * 查商品列表。
     * 走 Play 能拿到本地化的 formattedPrice（按用户所在区域自动换算），
     * 这比服务端写死的美元价更准确 —— 前端应优先用这个。
     */
    public void queryProducts() {
        ensureConnected(() -> {
            List<QueryProductDetailsParams.Product> list = new ArrayList<>();
            for (String id : PLAN_IDS) {
                list.add(QueryProductDetailsParams.Product.newBuilder()
                        .setProductId(id)
                        .setProductType(BillingClient.ProductType.INAPP)
                        .build());
            }
            QueryProductDetailsParams params = QueryProductDetailsParams.newBuilder()
                    .setProductList(list)
                    .build();

            client.queryProductDetailsAsync(params, (BillingResult r, List<ProductDetails> details) -> {
                if (r.getResponseCode() != BillingClient.BillingResponseCode.OK) {
                    Log.w(TAG, "查商品失败: " + r.getResponseCode() + " " + r.getDebugMessage());
                    if (callback != null) {
                        callback.onError("QUERY_FAILED", "获取商品信息失败");
                    }
                    return;
                }

                productCache.clear();
                JSONArray arr = new JSONArray();
                for (ProductDetails pd : details) {
                    productCache.put(pd.getProductId(), pd);
                    try {
                        JSONObject o = new JSONObject();
                        o.put("id", pd.getProductId());
                        o.put("title", pd.getName());
                        o.put("description", pd.getDescription());

                        // 一次性商品的价格在 oneTimePurchaseOfferDetails 里
                        String price = "";
                        if (pd.getOneTimePurchaseOfferDetails() != null) {
                            price = pd.getOneTimePurchaseOfferDetails().getFormattedPrice();
                        }
                        o.put("price", price);
                        arr.put(o);
                    } catch (Exception ignored) {
                    }
                }
                Log.i(TAG, "拿到 " + arr.length() + " 个商品");
                if (callback != null) callback.onProducts(arr.toString());
            });
        });
    }

    // ── 购买 ─────────────────────────────────────────────────

    public void buy(Activity activity, String planId) {
        ensureConnected(() -> {
            ProductDetails pd = productCache.get(planId);
            if (pd == null) {
                // 商品缓存为空（比如刚启动就点购买），先查再买
                if (callback != null) callback.onError("NOT_LOADED", "商品信息未就绪，请稍后重试");
                queryProducts();
                return;
            }

            List<BillingFlowParams.ProductDetailsParams> params = new ArrayList<>();
            params.add(BillingFlowParams.ProductDetailsParams.newBuilder()
                    .setProductDetails(pd)
                    .build());

            BillingResult r = client.launchBillingFlow(activity,
                    BillingFlowParams.newBuilder()
                            .setProductDetailsParamsList(params)
                            .build());

            if (r.getResponseCode() != BillingClient.BillingResponseCode.OK) {
                Log.w(TAG, "拉起付款失败: " + r.getResponseCode());
                if (callback != null) {
                    callback.onError("LAUNCH_FAILED", "无法打开付款界面（" + r.getResponseCode() + "）");
                }
            }
        });
    }

    /** 购买结果回调。只拿 token，不发 hearts。 */
    private void handlePurchaseUpdate(BillingResult result, List<Purchase> purchases) {
        int code = result.getResponseCode();

        if (code == BillingClient.BillingResponseCode.USER_CANCELED) {
            if (callback != null) callback.onError("USER_CANCELED", "已取消");
            return;
        }
        if (code != BillingClient.BillingResponseCode.OK) {
            Log.w(TAG, "购买失败: " + code + " " + result.getDebugMessage());
            if (callback != null) callback.onError("PURCHASE_FAILED", "购买失败（" + code + "）");
            return;
        }
        if (purchases == null || purchases.isEmpty()) {
            if (callback != null) callback.onError("NO_PURCHASE", "未获取到订单");
            return;
        }

        for (Purchase p : purchases) {
            // PENDING 表示延迟付款（比如便利店现金支付），此时不能发放
            if (p.getPurchaseState() != Purchase.PurchaseState.PURCHASED) {
                Log.i(TAG, "订单状态非 PURCHASED: " + p.getPurchaseState());
                continue;
            }
            String token = p.getPurchaseToken();
            String planId = p.getProducts().isEmpty() ? "" : p.getProducts().get(0);
            Log.i(TAG, "支付成功 plan=" + planId + " token=" + token.substring(0, Math.min(12, token.length())) + "...");
            if (callback != null) callback.onPurchaseToken(planId, token);
        }
    }

    // ── 消耗 ─────────────────────────────────────────────────

    /**
     * 标记已消费。必须在服务端发放 hearts 成功之后调用。
     * 不 consume 的话，用户买过一次就再也买不了同一档。
     */
    public void consume(String token) {
        if (!ready()) {
            Log.w(TAG, "consume 时未连接，跳过（下次启动会按未消费订单补发）");
            return;
        }
        client.consumeAsync(
                ConsumeParams.newBuilder().setPurchaseToken(token).build(),
                (BillingResult r, String t) -> {
                    if (r.getResponseCode() == BillingClient.BillingResponseCode.OK) {
                        Log.i(TAG, "已消费订单");
                    } else {
                        Log.w(TAG, "consume 失败: " + r.getResponseCode());
                    }
                });
    }

    // ── 恢复未消费订单 ───────────────────────────────────────

    /**
     * 查有没有「已付款但没发放」的订单。
     *
     * 典型场景：用户付款成功、前端请求服务端时断网了 →
     * hearts 没到账但钱扣了。下次启动跑一次这个，能把钱补回来。
     */
    public void restore() {
        ensureConnected(() -> client.queryPurchasesAsync(
                QueryPurchasesParams.newBuilder()
                        .setProductType(BillingClient.ProductType.INAPP)
                        .build(),
                (BillingResult r, List<Purchase> purchases) -> {
                    int n = 0;
                    if (r.getResponseCode() == BillingClient.BillingResponseCode.OK
                            && purchases != null) {
                        for (Purchase p : purchases) {
                            if (p.getPurchaseState() != Purchase.PurchaseState.PURCHASED) continue;
                            String planId = p.getProducts().isEmpty() ? "" : p.getProducts().get(0);
                            if (callback != null) {
                                callback.onPurchaseToken(planId, p.getPurchaseToken());
                                n++;
                            }
                        }
                    }
                    if (callback != null) callback.onRestoreDone(n);
                }));
    }

    public void destroy() {
        if (client != null) {
            try {
                client.endConnection();
            } catch (Exception ignored) {
            }
            client = null;
        }
    }
}
