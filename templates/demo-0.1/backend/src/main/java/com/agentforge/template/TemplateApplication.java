package com.agentforge.template;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 生成项目基础模板入口。
 *
 * <p>该模板与 AgentForge 平台自身的登录体系完全独立：登录用户来自本项目的
 * {@code app_user} 表，凭据由部署环境注入，不复用平台账号。
 */
@SpringBootApplication
@MapperScan("com.agentforge.template.mapper")
public class TemplateApplication {

    public static void main(String[] args) {
        SpringApplication.run(TemplateApplication.class, args);
    }
}
