package com.agentforge.integration.port;

/** 内部失败类型：供应商不可用、未知城市或响应无法解析。明确失败，不返回静默默认值。 */
public class WeatherUnavailableException extends RuntimeException {

    public WeatherUnavailableException(String message) {
        super(message);
    }

    public WeatherUnavailableException(String message, Throwable cause) {
        super(message, cause);
    }
}
