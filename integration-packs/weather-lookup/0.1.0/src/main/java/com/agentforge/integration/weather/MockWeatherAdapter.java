package com.agentforge.integration.weather;

import java.util.Map;

/**
 * Mock-only Adapter：**确定性**返回固定天气，不访问网络、不需要任何真实凭据。
 *
 * <p>外部响应属于**不可信数据**：这里给出的值只用于确定性验收，不能据此宣称 Live 可用。
 */
public final class MockWeatherAdapter implements WeatherPort {

    private static final Map<String, WeatherForecast> FIXTURES = Map.of(
            "beijing", new WeatherForecast("beijing", 21, "clear"),
            "shanghai", new WeatherForecast("shanghai", 24, "cloudy"),
            "shenzhen", new WeatherForecast("shenzhen", 28, "rain")
    );

    @Override
    public WeatherForecast getForecast(String city) {
        if (city == null || city.isBlank()) {
            throw new IllegalArgumentException("city 不能为空");
        }
        WeatherForecast known = FIXTURES.get(city.toLowerCase());
        if (known == null) {
            // "unknown_city" 场景：确定性失败，供 E02 契约测试断言失败路径。
            throw new UnknownCityException(city);
        }
        return known;
    }

    /** 未知城市：明确的失败类型，不静默返回默认值。 */
    public static final class UnknownCityException extends RuntimeException {
        public UnknownCityException(String city) {
            super("未知城市：" + city);
        }
    }
}
