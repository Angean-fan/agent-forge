package com.agentforge.template.util;

import java.util.Objects;

/** 必填字段校验工具（R06B 示例方法单元；不声称覆盖任意业务）。 */
public final class FieldValidator {

    private FieldValidator() {
    }

    public static void validateRequired(String field, Object value) {
        Objects.requireNonNull(field, "field");
        if (value == null || (value instanceof String text && text.isBlank())) {
            throw new IllegalArgumentException(field + " 不能为空");
        }
    }
}
