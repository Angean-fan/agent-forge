package com.agentforge.template.domain;

import java.util.Set;

/** 状态转移守卫（R06B 示例方法单元）。 */
public final class StatusGuard {

    private StatusGuard() {
    }

    public static void assertTransition(String from, String to, Set<String> allowed) {
        if (!allowed.contains(from + "->" + to)) {
            throw new IllegalStateException("非法状态转移：" + from + " -> " + to);
        }
    }
}
