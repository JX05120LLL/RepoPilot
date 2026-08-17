package com.repopilot.platform.auth.service;

import com.repopilot.platform.auth.dto.LoginRequest;
import com.repopilot.platform.auth.dto.RegisterRequest;
import com.repopilot.platform.auth.dto.TokenResponse;
import com.repopilot.platform.auth.security.JwtService;
import com.repopilot.platform.auth.user.User;
import com.repopilot.platform.auth.user.UserRepository;
import com.repopilot.platform.common.exception.ApiException;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class AuthService {

    private final UserRepository userRepository;
    private final PasswordEncoder passwordEncoder;
    private final JwtService jwtService;

    public AuthService(UserRepository userRepository, PasswordEncoder passwordEncoder, JwtService jwtService) {
        this.userRepository = userRepository;
        this.passwordEncoder = passwordEncoder;
        this.jwtService = jwtService;
    }

    @Transactional
    public TokenResponse register(RegisterRequest request) {
        if (userRepository.existsByUsername(request.username())) {
            throw ApiException.conflict("USERNAME_TAKEN", "用户名已被占用。");
        }
        if (userRepository.findByTenantIdAndEmail(request.tenantId(), request.email()).isPresent()) {
            throw ApiException.conflict("EMAIL_TAKEN", "该租户下邮箱已被注册。");
        }
        User user = new User(
                request.username(),
                request.email(),
                passwordEncoder.encode(request.password()),
                request.role(),
                request.tenantId()
        );
        userRepository.save(user);
        return issue(user);
    }

    @Transactional(readOnly = true)
    public TokenResponse login(LoginRequest request) {
        User user = userRepository.findByUsername(request.username())
                .orElseThrow(() -> ApiException.unauthorized("BAD_CREDENTIALS", "用户名或密码错误。"));
        if (!user.isEnabled()) {
            throw ApiException.forbidden("USER_DISABLED", "账号已禁用。");
        }
        if (!passwordEncoder.matches(request.password(), user.getPasswordHash())) {
            throw ApiException.unauthorized("BAD_CREDENTIALS", "用户名或密码错误。");
        }
        return issue(user);
    }

    private TokenResponse issue(User user) {
        String access = jwtService.generateAccessToken(user.getUsername(), user.getTenantId(), user.getRole());
        String refresh = jwtService.generateRefreshToken(user.getUsername(), user.getTenantId());
        return new TokenResponse(
                access,
                refresh,
                jwtService.accessTtlSeconds(),
                "Bearer",
                user.getUsername(),
                user.getRole(),
                user.getTenantId()
        );
    }
}
