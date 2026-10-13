package com.agentforge.integration.adapter;

/**
 * 传输层抽象：真实实现走 HTTP，测试可注入确定性 stub。返回值是第三方**原始响应体**——不可信数据。
 */
@FunctionalInterface
public interface WeatherTransport {

    /** @return 第三方原始响应体（不可信）；网络超时抛 {@link TimeoutException}。 */
    String get(String path);

    /** 传输超时，与业务失败区分。 */
    final class TimeoutException extends RuntimeException {
        public TimeoutException(String message) {
            super(message);
        }
    }
}
