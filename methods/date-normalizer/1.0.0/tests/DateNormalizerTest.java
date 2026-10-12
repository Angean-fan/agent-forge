package com.agentforge.template.util;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

class DateNormalizerTest {

    @Test
    void slashDateToIso() {
        assertEquals("2026-10-12", DateNormalizer.normalize("2026/10/12"));
    }

    @Test
    void rejectsUnparseable() {
        assertThrows(IllegalArgumentException.class, () -> DateNormalizer.normalize("not-a-date"));
    }
}
