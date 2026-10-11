package com.agentforge.template.exception;

/** 业务异常，由 GlobalExceptionHandler 统一转换为错误响应。 */
public class ApiException extends RuntimeException {

    private final String code;
    private final int status;

    public ApiException(String code, String message, int status) {
        super(message);
        this.code = code;
        this.status = status;
    }

    public String getCode() {
        return code;
    }

    public int getStatus() {
        return status;
    }
}
