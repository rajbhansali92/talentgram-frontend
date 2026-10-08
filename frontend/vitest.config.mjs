import { defineConfig } from "vitest/config";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
    resolve: {
        alias: {
            // Same mapping as jsconfig.json / next.config.js — must come before the generic "@".
            "@/pages": path.resolve(__dirname, "src/pages-components"),
            "@": path.resolve(__dirname, "src"),
        },
    },
    test: {
        environment: "jsdom",
        include: ["src/**/*.test.{js,jsx}"],
    },
});
