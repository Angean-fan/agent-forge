package com.agentforge.template.util;

import java.time.LocalDate;
import java.time.format.DateTimeFormatter;

/** 日期归一化（R06B 示例方法单元）。 */
public final class DateNormalizer {

    private static final DateTimeFormatter[] PARSERS = {
        DateTimeFormatter.ofPattern("yyyy/MM/dd"),
        DateTimeFormatter.ISO_LOCAL_DATE,
    };

    private DateNormalizer() {
    }

    public static String normalize(String raw) {
        for (DateTimeFormatter parser : PARSERS) {
            try {
                return LocalDate.parse(raw, parser).toString();
            } catch (RuntimeException ignored) {
                // 尝试下一个受支持的格式
            }
        }
        throw new IllegalArgumentException("无法解析日期：" + raw);
    }
}
