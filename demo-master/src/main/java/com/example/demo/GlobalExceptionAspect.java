package com.example.demo;

import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Pointcut;
import org.springframework.http.*;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;
import java.util.Objects;

@Aspect
@Component
public class GlobalExceptionAspect {

    @Pointcut("within(com.example.demo..*)")
    public void applicationPointcut() {
    }

    @Around("applicationPointcut()")
    public Object handleException(ProceedingJoinPoint joinPoint) throws Throwable {
        try {
            return joinPoint.proceed();
        } catch (Throwable e) {
            System.out.println("[AOP] 捕获到异常: " + e.getClass().getSimpleName());
            sendError(e);
            throw e;
        }
    }

    private void sendError(Throwable e) {
        try {
            RestTemplate rt = new RestTemplate();
            String url = "http://localhost:5000/api/error";

            StringBuilder stackTraceBuilder = new StringBuilder();
            for (StackTraceElement element : e.getStackTrace()) {
                stackTraceBuilder.append(element.toString()).append("\n");
            }
            String stackTrace = stackTraceBuilder.toString()
                    .replace("\\", "\\\\")
                    .replace("\"", "\\\"")
                    .replace("\n", "\\n")
                    .replace("\r", "\\r")
                    .replace("\t", "\\t");
            String errorMsg = Objects.toString(e.getMessage(), "");
            String errorMessage = e.getClass().getSimpleName() + ": " + errorMsg
                    .replace("\\", "\\\\")
                    .replace("\"", "\\\"")
                    .replace("\n", "\\n")
                    .replace("\r", "\\r")
                    .replace("\t", "\\t");

            StackTraceElement topElement = e.getStackTrace()[0];
            String className = topElement.getClassName();
            // 处理内部类：去掉 $ 及其后面的部分
            if (className.contains("$")) {
                className = className.substring(0, className.indexOf("$"));
            }
            String filePath = "demo-master/src/main/java/" + className.replace('.', '/') + ".java";

            String json = "{"
                    + "\"filePath\":\"" + filePath + "\","
                    + "\"errorMessage\":\"" + errorMessage + "\","
                    + "\"stackTrace\":\"" + stackTrace + "\""
                    + "}";

            System.out.println("[AOP] 发送的JSON: " + json);

            HttpHeaders headers = new HttpHeaders();
            headers.set("Content-Type", "application/json");
            HttpEntity<String> request = new HttpEntity<>(json, headers);
            rt.postForEntity(url, request, String.class);
            System.out.println("[AOP] 发送成功");

        } catch (Exception ex) {
            System.out.println("[AOP] 发送失败: " + ex.getMessage());
        }
    }
}
