package androidx.core.app;

import android.os.Bundle;
import android.os.IBinder;

/**
 * androidx.core.app.BundleCompat 的最小替代实现。
 *
 * 为什么需要这个文件
 * ------------------
 * com.android.billingclient 5.0.0 的 BillingClientImpl 有唯一一处
 * androidx 引用：BundleCompat.getBinder(Bundle, String)。
 * 原本它是 API 18 以下的兼容包装 —— API 18+ 直接调 Bundle.getBinder()。
 *
 * 本项目的 minSdk 是 24，所以真正需要的只是那一行转发。
 *
 * 为什么不用真的 androidx.core
 * ----------------------------
 * androidx.core AAR 有数百个类、约 1.5MB。为了一个方法引入它，
 * 会让这个「零第三方依赖、手写构建链」的项目背上完整依赖树。
 * 自己写 6 行是更合适的选择。
 *
 * 命名沿用 androidx.core.app 是刻意的：billing 的字节码里写的
 * 就是这个全限定名，类名必须完全一致才能被正确解析。
 */
public final class BundleCompat {
    private BundleCompat() {}

    public static IBinder getBinder(Bundle bundle, String key) {
        return bundle == null ? null : bundle.getBinder(key);
    }

    public static void putBinder(Bundle bundle, String key, IBinder binder) {
        if (bundle != null) bundle.putBinder(key, binder);
    }
}
