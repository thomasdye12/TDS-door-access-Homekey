#pragma once

constexpr char WIFI_SSID[] = "YOUR_WIFI_NAME";
constexpr char WIFI_PASSWORD[] = "YOUR_WIFI_PASSWORD";
constexpr char BACKEND_HOST[] = "door-access-controller.local";

// One random secret shared by this controller's fleet image. Each NodeMCU
// derives its own WebSocket/OTA credential as HMAC-SHA256(secret, Wi-Fi MAC).
constexpr char FLEET_SECRET[] = "REPLACE_WITH_A_LONG_RANDOM_FLEET_SECRET";
