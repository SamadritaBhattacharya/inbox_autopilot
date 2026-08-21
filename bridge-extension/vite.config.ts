import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

/**
 * Three separate entry points, each emitted at a fixed name.
 *
 * No hashing: `manifest.json` names these files literally, and a content-hashed bundle would
 * point the manifest at a file that no longer exists after every rebuild. No code splitting
 * either — a service worker cannot load a shared chunk lazily, and the "vendor chunk" a
 * default config produces makes the worker fail to register with an error that names neither.
 */
export default defineConfig({
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "chrome116",
    // The extension is loaded unpacked and read by humans when something misbehaves; a
    // minified service worker turns a five-minute diagnosis into an afternoon.
    minify: false,
    rollupOptions: {
      input: {
        background: fileURLToPath(new URL("./src/background.ts", import.meta.url)),
        popup: fileURLToPath(new URL("./src/ui/popup.ts", import.meta.url)),
        options: fileURLToPath(new URL("./src/ui/options.ts", import.meta.url)),
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "[name].js",
        assetFileNames: "[name].[ext]",
        inlineDynamicImports: false,
        manualChunks: undefined,
      },
    },
  },
});
