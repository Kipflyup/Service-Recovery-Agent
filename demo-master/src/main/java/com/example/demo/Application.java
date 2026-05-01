package com.example.demo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import java.util.Objects;

@SpringBootApplication(exclude = {DataSourceAutoConfiguration.class})
public class Application {

    public static void main(String[] args) {
        // 修复参数空指针风险，显式处理null参数
        String[] runArgs = Objects.requireNonNullElseGet(args, () -> new String[0]);
        SpringApplication.run(Application.class, runArgs);
    }

    @RestController
    public static class TestController {
        @GetMapping("/test")
        public String testException() {
            System.out.println("starting...");
            Object obj = null;
            // 保留原有空判断逻辑，避免空指针
            if (obj != null) {
                obj.toString();
            }
            return "ok";
        }
    }
}