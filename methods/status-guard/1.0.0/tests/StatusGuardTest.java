package com.agentforge.template.domain;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.util.Set;
import org.junit.jupiter.api.Test;

class StatusGuardTest {

    private static final Set<String> ALLOWED = Set.of("OPEN->CLOSED");

    @Test
    void rejectsIllegalTransition() {
        assertThrows(IllegalStateException.class,
            () -> StatusGuard.assertTransition("CLOSED", "OPEN", ALLOWED));
    }

    @Test
    void acceptsAllowedTransition() {
        assertDoesNotThrow(() -> StatusGuard.assertTransition("OPEN", "CLOSED", ALLOWED));
    }
}
