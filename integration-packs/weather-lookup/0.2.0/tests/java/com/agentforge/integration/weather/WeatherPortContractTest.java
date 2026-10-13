package com.agentforge.integration.weather;

import com.agentforge.integration.adapter.HttpWeatherAdapter;
import com.agentforge.integration.adapter.WeatherTransport;
import com.agentforge.integration.mock.MockWeatherAdapter;
import com.agentforge.integration.port.WeatherForecast;
import com.agentforge.integration.port.WeatherPort;
import com.agentforge.integration.port.WeatherTimeoutException;
import com.agentforge.integration.port.WeatherUnavailableException;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * {@link WeatherPort} 契约测试：**任何**实现（Mock 与真实/stub Adapter）都必须满足同一接口约束。
 *
 * <p>Mock 模式下覆盖 success、failure、timeout 三种场景；Mock 成功不代表 Live 可用——
 * 这里只断言接口与错误语义，不证明真实鉴权、网络或厂商审批。
 */
class WeatherPortContractTest {

    /** 成功场景：返回内部类型、字段有效、结果确定性。 */
    @Test
    void successReturnsInternalForecast() {
        WeatherPort port = new MockWeatherAdapter(MockWeatherAdapter.Scenario.SUCCESS);
        WeatherForecast forecast = port.getForecast("Beijing");

        assertNotNull(forecast);
        assertEquals("beijing", forecast.city());
        assertTrue(forecast.temperatureCelsius() > -100 && forecast.temperatureCelsius() < 100);
        assertEquals(forecast, port.getForecast("beijing"));
    }

    /** 失败场景：明确的失败类型，不返回静默默认值。 */
    @Test
    void failureIsTypedFailure() {
        WeatherPort port = new MockWeatherAdapter(MockWeatherAdapter.Scenario.FAILURE);

        assertThrows(WeatherUnavailableException.class, () -> port.getForecast("beijing"));
    }

    /** 超时场景：与普通失败区分（业务侧可据此决定重试）。 */
    @Test
    void timeoutIsTypedTimeout() {
        WeatherPort port = new MockWeatherAdapter(MockWeatherAdapter.Scenario.TIMEOUT);

        assertThrows(WeatherTimeoutException.class, () -> port.getForecast("beijing"));
    }

    /** 输入契约：空城市一律拒绝，Mock 与真实 Adapter 行为一致。 */
    @Test
    void blankCityIsAlwaysRejected() {
        WeatherPort[] adapters = {
                new MockWeatherAdapter(MockWeatherAdapter.Scenario.SUCCESS),
                new HttpWeatherAdapter(path -> "{\"temperatureCelsius\":21,\"condition\":\"clear\"}")
        };
        for (WeatherPort port : adapters) {
            assertThrows(IllegalArgumentException.class, () -> port.getForecast("  "));
        }
    }

    /** 真实 Adapter 把不可信 payload 归一化为内部类型（不向上传播供应商 DTO）。 */
    @Test
    void realAdapterParsesUntrustedPayloadIntoInternalType() {
        WeatherTransport stub = path -> "{\"temperatureCelsius\":21,\"condition\":\"clear\"}";

        WeatherForecast forecast = new HttpWeatherAdapter(stub).getForecast("beijing");

        assertEquals(21, forecast.temperatureCelsius());
        assertEquals("clear", forecast.condition());
    }

    /** 真实 Adapter 把传输超时映射为内部超时类型。 */
    @Test
    void realAdapterMapsVendorTimeoutToTypedTimeout() {
        WeatherTransport stub = path -> {
            throw new WeatherTransport.TimeoutException("slow");
        };

        assertThrows(WeatherTimeoutException.class,
                () -> new HttpWeatherAdapter(stub).getForecast("beijing"));
    }

    /** 真实 Adapter 对无法解析的响应明确失败，不吞异常。 */
    @Test
    void realAdapterRejectsMalformedPayload() {
        WeatherTransport stub = path -> "<html>502</html>";

        assertThrows(WeatherUnavailableException.class,
                () -> new HttpWeatherAdapter(stub).getForecast("beijing"));
    }
}
