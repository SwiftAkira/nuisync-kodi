export { RelayRoom } from "./relay-room.js";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Health check
    if (url.pathname === "/") {
      return new Response("NuiSync relay is running~");
    }

    // Match /room/{roomId}
    const match = url.pathname.match(/^\/room\/([A-Za-z0-9]{3,10})$/);
    if (!match) {
      return new Response("Not found", { status: 404 });
    }

    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("Expected WebSocket", { status: 426 });
    }

    const roomId = match[1].toUpperCase();
    const id = env.ROOMS.idFromName(roomId);
    const stub = env.ROOMS.get(id);
    return stub.fetch(request);
  },
};