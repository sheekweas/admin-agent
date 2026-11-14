const config = {
  output: 'standalone',
  poweredByHeader: false,
  // Optional local E2E proxy permits testing a fresh build on another port.
  ...(process.env.E2E_API_URL ? {
    async rewrites() {
      return [{ source: '/e2e-api/:path*', destination: `${process.env.E2E_API_URL}/:path*` }];
    },
  } : {}),
};
export default config;
