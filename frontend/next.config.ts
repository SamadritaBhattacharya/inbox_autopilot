import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // @inbox/contracts is a workspace package published as untranspiled ESM.
  transpilePackages: ["@inbox/contracts"],
};

export default nextConfig;
