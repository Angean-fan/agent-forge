package com.agentforge.template.controller;

import com.agentforge.template.dto.LoginRequest;
import com.agentforge.template.service.AuthService;
import com.agentforge.template.vo.LoginResponse;
import com.agentforge.template.vo.UserView;
import jakarta.validation.Valid;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/auth")
public class AuthController {

    private final AuthService authService;

    public AuthController(AuthService authService) {
        this.authService = authService;
    }

    @PostMapping("/login")
    public LoginResponse login(@Valid @RequestBody LoginRequest request) {
        return authService.login(request.username(), request.password());
    }

    @GetMapping("/me")
    public UserView me(Authentication authentication) {
        return authService.currentUser(authentication.getName());
    }
}
