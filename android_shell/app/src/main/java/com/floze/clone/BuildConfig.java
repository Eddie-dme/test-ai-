package com.floze.clone;

/**
 * 由 Gradle 生成的文件，这里手工提供（因为本项目用 SDK 原生命令行构建）。
 *
 * SERVER_URL —— 测试服务器地址。
 *   改成跑 server.py 那台机器的局域网 IP。
 *   运行时可长按 App 标题栏临时修改（存 SharedPreferences，优先级更高）。
 */
public final class BuildConfig {
    public static final boolean DEBUG = true;
    public static final String APPLICATION_ID = "com.floze.clone";
    public static final int VERSION_CODE = 1;
    public static final String VERSION_NAME = "0.1.0";
    public static final String SERVER_URL = "http://192.168.71.82:8080";

    private BuildConfig() {}
}
