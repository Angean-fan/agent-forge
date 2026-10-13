package com.agentforge.integration.weather;

/**
 * 业务侧只依赖的**内部 Port**：生成的 Service 通过本接口取天气，不直接接触第三方协议。
 * E02 将由本 Port 落成 Service 的依赖点，Adapter 负责实现第三方协议。
 */
public interface WeatherPort {

    /**
     * 查询指定城市的天气。
     *
     * @param city 城市名（非空）
     * @return 归一化后的天气预报
     */
    WeatherForecast getForecast(String city);
}
