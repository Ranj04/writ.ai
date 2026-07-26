import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteStaticCopy } from "vite-plugin-static-copy";

export default defineConfig({
  plugins: [
    react(),
    viteStaticCopy({
      targets: [
        {
          src: "node_modules/tesseract.js/dist/worker.min.js",
          dest: "tesseract",
        },
        {
          src: "node_modules/tesseract.js-core/tesseract-core-lstm.wasm.js",
          dest: "tesseract/core",
        },
        {
          src: "node_modules/tesseract.js-core/tesseract-core-simd-lstm.wasm.js",
          dest: "tesseract/core",
        },
        {
          src: "node_modules/tesseract.js-core/tesseract-core-relaxedsimd-lstm.wasm.js",
          dest: "tesseract/core",
        },
        {
          src: "node_modules/@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz",
          dest: "tesseract/lang",
        },
      ],
    }),
  ],
  envDir: "..",
  server: { port: 5173, strictPort: true },
});
