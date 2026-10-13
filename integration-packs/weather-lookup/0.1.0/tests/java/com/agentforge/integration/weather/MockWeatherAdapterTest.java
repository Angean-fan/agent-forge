package com.agentforge.integration.weather;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

/** Mock Adapter 的确定性行为：成功与未知城市两条路径。 */
class MockWeatherAdapterTest {

    private final WeatherPort port = new MockWeatherAdapter();

    @Test
    void returnsDeterministicForecastForKnownCity() {
        WeatherForecast first = port.getForecast("beijing");
        WeatherForecast second = port.getForecast("Beijing");

        assertEquals("beijing", first.city());
        assertEquals(21, first.temperatureCelsius());
        assertEquals(first, second);
    }

    @Test
    void rejectsUnknownCityWithoutSilentDefault() {
        assertThrows(MockWeatherAdapter.UnknownCityException.class, () -> port.getForecast("atlantis"));
    }
}
