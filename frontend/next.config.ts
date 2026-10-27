import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.INTERNAL_API_URL ?? "http://backend:8000"}/api/:path*`,
      },
      {
        source: "/local-api/:path*",
        destination: `${process.env.INTERNAL_API_URL ?? "http://backend:8000"}/:path*`,
      },
    ];
  },
};

export default nextConfig;
