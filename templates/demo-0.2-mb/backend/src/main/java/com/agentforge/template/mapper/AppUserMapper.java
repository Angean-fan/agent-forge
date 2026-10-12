package com.agentforge.template.mapper;

import com.agentforge.template.entity.AppUser;
import org.apache.ibatis.annotations.Param;

/** MyBatis 映射接口（SQL 见 resources/mapper/AppUserMapper.xml）。 */
public interface AppUserMapper {

    AppUser findByUsername(@Param("username") String username);

    long countByUsername(@Param("username") String username);

    int insert(AppUser user);
}
