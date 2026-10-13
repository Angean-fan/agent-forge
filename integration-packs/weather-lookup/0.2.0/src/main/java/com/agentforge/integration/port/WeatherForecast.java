package com.agentforge.integration.port;

/** 内部领域视图：第三方天气响应归一化后的结果。核心业务规则只认识本类型，不接触供应商 DTO。 */
public record WeatherForecast(String city, int temperatureCelsius, String condition) {
}
