package com.agentforge.integration.adapter;

import com.agentforge.integration.port.WeatherForecast;
import com.agentforge.integration.port.WeatherPort;
import com.agentforge.integration.port.WeatherTimeoutException;
import com.agentforge.integration.port.WeatherUnavailableException;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 协议 Adapter：实现第三方天气协议，把**不可信的原始响应**归一化为内部 {@link WeatherForecast}。
 *
 * <p>把供应商 DTO / 原始文本挡在 Port 之内：只提取字段并校验；解析失败或超时即明确失败，
 * 不把供应商对象或原始响应向上传播到核心业务规则。
 */
public final class HttpWeatherAdapter implements WeatherPort {

    private static final Pattern TEMPERATURE = Pattern.compile("\"temperatureCelsius\"\\s*:\\s*(-?\\d+)");
    private static final Pattern CONDITION = Pattern.compile("\"condition\"\\s*:\\s*\"([a-z0-9_]+)\"");

    private final WeatherTransport transport;

    public HttpWeatherAdapter(WeatherTransport transport) {
        this.transport = transport;
    }

    @Override
    public WeatherForecast getForecast(String city) {
        if (city == null || city.isBlank()) {
            throw new IllegalArgumentException("city 不能为空");
        }
        String body;
        try {
            body = transport.get("/forecast?city=" + city.toLowerCase());
        } catch (WeatherTransport.TimeoutException timeout) {
            throw new WeatherTimeoutException("vendor timeout", timeout);
        }
        Matcher temperature = TEMPERATURE.matcher(body);
        Matcher condition = CONDITION.matcher(body);
        if (!temperature.find() || !condition.find()) {
            throw new WeatherUnavailableException("malformed vendor payload");
        }
        return new WeatherForecast(
                city.toLowerCase(), Integer.parseInt(temperature.group(1)), condition.group(1));
    }
}
