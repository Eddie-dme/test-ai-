package top.lurvy.vesperine;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

/**
 * Vesperine —— 主 Activity
 *
 * 极简 WebView 壳：
 *   - 加载内嵌的 Web 前端（assets/www）
 *   - 保留 DOM storage（前端用 localStorage 存偏好）
 *   - 允许 cleartext（测试环境是 http，正式环境应改 https）
 *   - 标题栏右侧有常驻的「服务器设置」按钮；长按标题栏同样可改
 *   - 暴露 JavascriptInterface，供前端在连不上服务时唤起地址设置
 */
public class MainActivity extends Activity {

    private static final String PREF = "vesperine_prefs";
    private static final String KEY_URL = "server_url";

    private WebView web;
    private ProgressBar bar;
    private TextView title;
    private SharedPreferences prefs;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        prefs = getSharedPreferences(PREF, Context.MODE_PRIVATE);

        // ---- 布局：标题栏（标题 + 设置按钮）+ 进度条 + WebView ----
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);

        LinearLayout topBar = new LinearLayout(this);
        topBar.setOrientation(LinearLayout.HORIZONTAL);
        topBar.setBackgroundColor(Color.parseColor("#161324"));
        topBar.setGravity(Gravity.CENTER_VERTICAL);

        title = new TextView(this);
        title.setTextColor(Color.parseColor("#e8e3f5"));
        title.setTextSize(14f);
        title.setPadding(28, 22, 12, 22);
        title.setText(R.string.app_name);
        title.setSingleLine(true);
        title.setEllipsize(android.text.TextUtils.TruncateAt.MIDDLE);
        title.setOnLongClickListener(v -> {
            promptForUrl();
            return true;
        });
        topBar.addView(title, new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        // 常驻设置入口 —— 长按是隐藏手势，用户不可能发现，所以给个显式按钮
        TextView btnGear = new TextView(this);
        btnGear.setText("⚙");
        btnGear.setTextSize(17f);
        btnGear.setTextColor(Color.parseColor("#9a92b8"));
        btnGear.setPadding(30, 22, 30, 22);
        btnGear.setTypeface(Typeface.DEFAULT);
        btnGear.setOnClickListener(v -> promptForUrl());
        topBar.addView(btnGear, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        root.addView(topBar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(100);
        bar.setVisibility(View.GONE);
        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 6));

        FrameLayout holder = new FrameLayout(this);
        root.addView(holder, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        web = new WebView(this);
        holder.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        // ---- WebView 配置 ----
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);           // 前端用 localStorage
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);

        // 内嵌前端走 file:// 协议，需允许它访问 http:// 上的 API。
        // 服务端已配 CORS 头，这里放开 WebView 侧的限制。
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        try {
            s.setAllowUniversalAccessFromFileURLs(true);
        } catch (Throwable ignored) {
        }

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {
                v.loadUrl(url);                 // 站内跳转不交给系统浏览器
                return true;
            }
        });

        // 前端在连不上服务时可以调 VesperineNative.openServerSettings()
        // 直接唤起原生的地址设置对话框，不必让用户去猜长按标题栏。
        web.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public void openServerSettings() {
                runOnUiThread(MainActivity.this::promptForUrl);
            }
        }, "VesperineNative");
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView v, int p) {
                bar.setVisibility(p < 100 ? View.VISIBLE : View.GONE);
                bar.setProgress(p);
            }
        });

        String url = prefs.getString(KEY_URL, BuildConfig.SERVER_URL);
        loadApp(url);
    }

    /**
     * 加载 App。
     *
     * 优先用**内嵌资源**（assets/www）——启动快、立绘本地读取，
     * 通过 ?api= 参数告诉前端后端地址。
     * 若内嵌资源不可用，回退到直接加载远程页面。
     */
    private void loadApp(String serverUrl) {
        title.setText(getString(R.string.app_name) + "  ·  " + serverUrl);
        if (assetExists("www/index.html")) {
            String local = "file:///android_asset/www/index.html?api="
                    + android.net.Uri.encode(serverUrl);
            // 访问密钥随 URL 传给前端；为空则不加（服务端未配置时不校验）
            if (BuildConfig.ACCESS_KEY != null && !BuildConfig.ACCESS_KEY.isEmpty()) {
                local += "&key=" + android.net.Uri.encode(BuildConfig.ACCESS_KEY);
            }
            web.loadUrl(local);
        } else {
            web.loadUrl(serverUrl);
        }
    }

    private boolean assetExists(String path) {
        try {
            getAssets().open(path).close();
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /** 修改服务器地址（设置按钮 / 长按标题栏 / 前端桥接三个入口共用） */
    private void promptForUrl() {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(prefs.getString(KEY_URL, BuildConfig.SERVER_URL));
        input.setSelectAllOnFocus(true);

        LinearLayout wrap = new LinearLayout(this);
        wrap.setOrientation(LinearLayout.VERTICAL);
        wrap.setPadding(48, 28, 48, 8);

        TextView hint = new TextView(this);
        hint.setText(R.string.server_hint);
        hint.setTextSize(12.5f);
        hint.setTextColor(Color.parseColor("#9a92b8"));
        hint.setPadding(0, 0, 0, 18);
        wrap.addView(hint);

        wrap.addView(input, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        new AlertDialog.Builder(this)
                .setTitle(R.string.change_server)
                .setView(wrap)
                .setPositiveButton(android.R.string.ok, (d, w) -> {
                    String u = input.getText().toString().trim();
                    if (u.isEmpty()) return;
                    if (!u.startsWith("http")) u = "http://" + u;
                    prefs.edit().putString(KEY_URL, u).apply();
                    loadApp(u);
                    Toast.makeText(this, R.string.server_saved, Toast.LENGTH_SHORT).show();
                })
                .setNeutralButton(R.string.server_reset, (d, w) -> {
                    prefs.edit().remove(KEY_URL).apply();
                    loadApp(BuildConfig.SERVER_URL);
                    Toast.makeText(this, R.string.server_saved, Toast.LENGTH_SHORT).show();
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
