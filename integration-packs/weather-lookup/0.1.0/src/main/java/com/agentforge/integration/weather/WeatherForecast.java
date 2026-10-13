package com.agentforge.integration.weather;

/** 内部领域视图：第三方天气响应归一化后的结果，不向上暴露供应商 DTO。 */
public record WeatherForecast(String city, int temperatureCelsius, String condition) {
}
