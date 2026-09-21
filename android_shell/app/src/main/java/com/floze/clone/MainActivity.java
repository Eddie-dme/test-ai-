package com.floze.clone;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
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
 * FlOZE 复刻 —— 主 Activity
 *
 * 极简 WebView 壳：
 *   - 加载测试服务器上的 Web 前端
 *   - 保留 DOM storage（前端用 localStorage 存偏好）
 *   - 允许 cleartext（测试环境是 http，正式环境应改 https）
 *   - 长按顶部标题栏可修改服务器地址
 */
public class MainActivity extends Activity {

    private static final String PREF = "floze_prefs";
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

        // ---- 布局：标题条 + 进度条 + WebView ----
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);

        title = new TextView(this);
        title.setBackgroundColor(Color.parseColor("#161324"));
        title.setTextColor(Color.parseColor("#e8e3f5"));
        title.setTextSize(14f);
        title.setPadding(28, 22, 28, 22);
        title.setText(R.string.app_name);
        title.setOnLongClickListener(v -> {
            promptForUrl();
            return true;
        });
        root.addView(title, new LinearLayout.LayoutParams(
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

    /** 长按标题栏修改服务器地址（方便换测试机） */
    private void promptForUrl() {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(prefs.getString(KEY_URL, BuildConfig.SERVER_URL));
        input.setSelectAllOnFocus(true);

        LinearLayout wrap = new LinearLayout(this);
        wrap.setPadding(48, 32, 48, 16);
        wrap.addView(input, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        new android.app.AlertDialog.Builder(this)
                .setTitle(R.string.change_server)
                .setView(wrap)
                .setPositiveButton(android.R.string.ok, (d, w) -> {
                    String u = input.getText().toString().trim();
                    if (!u.startsWith("http")) u = "http://" + u;
                    prefs.edit().putString(KEY_URL, u).apply();
                    loadApp(u);
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
