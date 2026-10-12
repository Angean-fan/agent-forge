package com.agentforge.template.util;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

class FieldValidatorTest {

    @Test
    void rejectsBlankValue() {
        assertThrows(IllegalArgumentException.class,
            () -> FieldValidator.validateRequired("title", "   "));
    }

    @Test
    void acceptsPresentValue() {
        assertDoesNotThrow(() -> FieldValidator.validateRequired("title", "ok"));
    }
}
