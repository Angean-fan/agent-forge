package com.agentforge.integration.port;

/** 内部超时类型：与普通失败区分，便于业务侧决定是否重试；与 Live 是否可用无关。 */
public class WeatherTimeoutException extends WeatherUnavailableException {

    public WeatherTimeoutException(String message) {
        super(message);
    }

    public WeatherTimeoutException(String message, Throwable cause) {
        super(message, cause);
    }
}
