package com.repopilot.platform.common.exception;

import org.springframework.http.HttpStatus;

/**
 * 平台统一业务异常：携带稳定错误码与 HTTP 状态。
 * 错误码稳定不变，供前端与 Python 引擎按码分支，而非按文案判断。
 */
public class ApiException extends RuntimeException {

    private final HttpStatus status;
    private final String code;

    public ApiException(HttpStatus status, String code, String message) {
        super(message);
        this.status = status;
        this.code = code;
    }

    public HttpStatus getStatus() {
        return status;
    }

    public String getCode() {
        return code;
    }

    public static ApiException unauthorized(String code, String message) {
        return new ApiException(HttpStatus.UNAUTHORIZED, code, message);
    }

    public static ApiException forbidden(String code, String message) {
        return new ApiException(HttpStatus.FORBIDDEN, code, message);
    }

    public static ApiException notFound(String code, String message) {
        return new ApiException(HttpStatus.NOT_FOUND, code, message);
    }

    public static ApiException conflict(String code, String message) {
        return new ApiException(HttpStatus.CONFLICT, code, message);
    }

    public static ApiException badRequest(String code, String message) {
        return new ApiException(HttpStatus.BAD_REQUEST, code, message);
    }
}
