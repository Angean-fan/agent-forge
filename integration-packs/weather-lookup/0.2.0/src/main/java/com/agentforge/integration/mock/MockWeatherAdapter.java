package com.agentforge.integration.mock;

import com.agentforge.integration.port.WeatherForecast;
import com.agentforge.integration.port.WeatherPort;
import com.agentforge.integration.port.WeatherTimeoutException;
import com.agentforge.integration.port.WeatherUnavailableException;

import java.util.Map;

/**
 * Mock 实现：**确定性**、不访问网络、不需要任何真实凭据。按 {@link Scenario} 演示
 * success / failure / timeout 三条路径，用于 Mock 模式下的 Port 契约与业务闭环测试。
 *
 * <p>Mock 通过**不**代表 Live 可用：这里返回的固定值只证明接口与错误语义，不证明真实鉴权/网络。
 */
public final class MockWeatherAdapter implements WeatherPort {

    /** Mock 场景：Mock 模式下可确定性地演示成功、失败、超时。 */
    public enum Scenario { SUCCESS, FAILURE, TIMEOUT }

    private static final Map<String, WeatherForecast> FIXTURES = Map.of(
            "beijing", new WeatherForecast("beijing", 21, "clear"),
            "shanghai", new WeatherForecast("shanghai", 24, "cloudy"),
            "shenzhen", new WeatherForecast("shenzhen", 28, "rain")
    );

    private final Scenario scenario;

    public MockWeatherAdapter(Scenario scenario) {
        this.scenario = scenario;
    }

    @Override
    public WeatherForecast getForecast(String city) {
        if (city == null || city.isBlank()) {
            throw new IllegalArgumentException("city 不能为空");
        }
        return switch (scenario) {
            case TIMEOUT -> throw new WeatherTimeoutException("mock: 供应商超时");
            case FAILURE -> throw new WeatherUnavailableException("mock: 供应商失败");
            case SUCCESS -> {
                WeatherForecast forecast = FIXTURES.get(city.toLowerCase());
                if (forecast == null) {
                    throw new WeatherUnavailableException("mock: 未知城市 " + city);
                }
                yield forecast;
            }
        };
    }
}
