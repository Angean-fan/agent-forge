package com.agentforge.template.service;

import com.agentforge.template.entity.AppUser;
import com.agentforge.template.mapper.AppUserMapper;
import org.springframework.stereotype.Service;

@Service
public class UserService {

    private final AppUserMapper userMapper;

    public UserService(AppUserMapper userMapper) {
        this.userMapper = userMapper;
    }

    public AppUser findByUsername(String username) {
        return userMapper.findByUsername(username);
    }
}
