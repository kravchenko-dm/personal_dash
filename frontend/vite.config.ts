import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  define: { __BUILD_TIME__: JSON.stringify(new Date().toISOString()) },
  plugins: [
    react(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      registerType: "autoUpdate",
      injectRegister: "auto",
      manifest: {
        name: "Personal Dash",
        short_name: "Dash",
        description: "Задачи, балансы, календарь, YouGile и Telegram-саммари",
        lang: "ru",
        start_url: "/",
        scope: "/",
        display: "standalone",
        background_color: "#0f1115",
        theme_color: "#0f1115",
        icons: [
          { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
          { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" }
        ]
      },
      injectManifest: { globPatterns: ["**/*.{js,css,html,svg,png,ico,woff2}"] },
      devOptions: { enabled: true, type: "module" }
    })
  ],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000" }
  }
});
