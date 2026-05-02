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
        SpringApplication.run(Objects.requireNonNull(Application.class), Objects.requireNonNullElse(args, new String[0]));
    }

    @RestController
    public static class TestController {
        @GetMapping("/test")
        public String testException() {
            System.out.println("starting...");
            Object obj = null;
            // 保留原有空判断逻辑，避免空指针
            if (Objects.nonNull(obj)) {
                obj.toString();
            }
            return "ok";
        }
    }
}