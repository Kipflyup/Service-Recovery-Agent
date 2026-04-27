package com.example.demo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.jdbc.autoconfigure.DataSourceAutoConfiguration;
import org.springframework.http.*;
import org.springframework.web.client.RestTemplate;
import java.util.*;

@SpringBootApplication
@EnableAutoConfiguration(exclude = {DataSourceAutoConfiguration.class})
public class Application {

    public static void main(String[] args) {
        SpringApplication.run(Application.class, args);

        testWebhook();
    }

    static void testWebhook() {
        System.out.println("starting...");

        try {
            Object obj = null;
            // 添加null检查，避免空指针异常
            if (obj != null) {
                obj.toString();
            } else {
                System.out.println("obj为null，无需调用toString方法");
            }

        } catch (NullPointerException e) {
            System.out.println("捕获到异常");
            sendError(e);

        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    static void sendError(Exception e) {
        try {
            RestTemplate rt = new RestTemplate();
            String url = "http://localhost:5000/api/error";


            StringBuilder stackTraceBuilder = new StringBuilder();
            for (StackTraceElement element : e.getStackTrace()) {
                stackTraceBuilder.append(element.toString()).append("\n");
            }
            String stackTrace = stackTraceBuilder.toString().replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t");
            String errorMsg = Objects.toString(e.getMessage(), "");
            String errorMessage = e.getClass().getSimpleName() + ": " + errorMsg.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t");


            String json = "{"
                    + "\"filePath\":\"src/main/java/com/example/demo/Application.java\","
                    + "\"errorMessage\":\"" + errorMessage + "\","
                    + "\"stackTrace\":\"" + stackTrace + "\""
                    + "}";

            System.out.println("发送的JSON: " + json);

            HttpHeaders headers = new HttpHeaders();
            headers.set("Content-Type", "application/json");

            HttpEntity<String> request = new HttpEntity<>(json, headers);

            rt.postForEntity(url, request, String.class);
            System.out.println("发送成功");

        } catch (Exception ex) {
            System.out.println("发送失败: " + ex.getMessage());
        }
    }
}