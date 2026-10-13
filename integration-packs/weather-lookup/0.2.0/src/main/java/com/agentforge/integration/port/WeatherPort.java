package com.agentforge.integration.port;

/**
 * 业务侧**只依赖的内部 Port**：生成的服务通过它取天气。Adapter 才依赖第三方协议/SDK。
 *
 * <p>方法签名只引用内部类型（如 {@link WeatherForecast}），供应商 DTO 不得出现在此接口上。
 */
public interface WeatherPort {

    /**
     * 查询指定城市的天气。
     *
     * @param city 城市名（非空）
     * @return 归一化后的内部天气预报
     * @throws IllegalArgumentException      city 为空
     * @throws WeatherUnavailableException   供应商不可用 / 未知城市 / 响应无法解析
     * @throws WeatherTimeoutException        调用超时
     */
    WeatherForecast getForecast(String city);
}
