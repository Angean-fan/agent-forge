package com.agentforge.template.config;

import com.agentforge.template.entity.AppUser;
import com.agentforge.template.mapper.AppUserMapper;
import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.crypto.password.PasswordEncoder;

/** 从部署环境注入的凭据初始化管理员账号；未配置则跳过（不硬编码凭据）。 */
@Configuration
public class AdminUserInitializer {

    private static final Logger log = LoggerFactory.getLogger(AdminUserInitializer.class);

    @Bean
    public ApplicationRunner seedAdminUser(
            AppUserMapper userMapper,
            PasswordEncoder passwordEncoder,
            @Value("${app.security.admin.username:}") String username,
            @Value("${app.security.admin.password:}") String password) {
        return args -> {
            if (username == null || username.isBlank() || password == null || password.isBlank()) {
                log.warn("APP_ADMIN_USERNAME/APP_ADMIN_PASSWORD 未配置，跳过管理员初始化");
                return;
            }
            Long existing = userMapper.selectCount(
                    new QueryWrapper<AppUser>().eq("username", username));
            if (existing != null && existing > 0) {
                return;
            }
            AppUser admin = new AppUser();
            admin.setUsername(username);
            admin.setPasswordHash(passwordEncoder.encode(password));
            admin.setRole("ADMIN");
            admin.setEnabled(true);
            userMapper.insert(admin);
            log.info("已初始化管理员账号: {}", username);
        };
    }
}
