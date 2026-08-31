import { defineMiddleware } from "astro:middleware";

export const onRequest = defineMiddleware((context, next) => {
  if (!import.meta.env.DEV || !context.url.pathname.endsWith(".html")) {
    return next();
  }

  const pathname = context.url.pathname === "/index.html"
    ? "/"
    : context.url.pathname.slice(0, -".html".length);

  return next(`${pathname}${context.url.search}`);
});
