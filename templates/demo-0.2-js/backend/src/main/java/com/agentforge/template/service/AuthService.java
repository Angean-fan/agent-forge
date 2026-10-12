package com.agentforge.template.service;

import com.agentforge.template.entity.AppUser;
import com.agentforge.template.exception.ApiException;
import com.agentforge.template.exception.ErrorCode;
import com.agentforge.template.security.JwtService;
import com.agentforge.template.vo.LoginResponse;
import com.agentforge.template.vo.UserView;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;

@Service
public class AuthService {

    private final UserService userService;
    private final PasswordEncoder passwordEncoder;
    private final JwtService jwtService;

    public AuthService(UserService userService, PasswordEncoder passwordEncoder, JwtService jwtService) {
        this.userService = userService;
        this.passwordEncoder = passwordEncoder;
        this.jwtService = jwtService;
    }

    public LoginResponse login(String username, String password) {
        AppUser user = userService.findByUsername(username);
        boolean ok = user != null
                && Boolean.TRUE.equals(user.getEnabled())
                && passwordEncoder.matches(password, user.getPasswordHash());
        if (!ok) {
            throw new ApiException(ErrorCode.UNAUTHORIZED, "用户名或密码错误", 401);
        }
        String token = jwtService.generate(user.getUsername(), user.getRole());
        return new LoginResponse(
                token, new UserView(user.getId(), user.getUsername(), user.getRole()));
    }

    public UserView currentUser(String username) {
        AppUser user = userService.findByUsername(username);
        if (user == null) {
            throw new ApiException(ErrorCode.UNAUTHORIZED, "用户不存在", 401);
        }
        return new UserView(user.getId(), user.getUsername(), user.getRole());
    }
}
