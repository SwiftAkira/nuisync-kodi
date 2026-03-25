import { DurableObject } from "cloudflare:workers";

export class RelayRoom extends DurableObject {
  async fetch(request) {
    const websockets = this.ctx.getWebSockets();
    if (websockets.length >= 2) {
      return new Response("Room full", { status: 403 });
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    const role = websockets.length === 0 ? "host" : "guest";
    this.ctx.acceptWebSocket(server);
    server.serializeAttachment({ role });

    // Tell the new client their role
    server.send(JSON.stringify({ cmd: "_role", role }));

    // Tell existing client that peer joined
    for (const ws of websockets) {
      ws.send(JSON.stringify({ cmd: "_peer_joined" }));
    }

    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, message) {
    // Relay to the other client
    for (const peer of this.ctx.getWebSockets()) {
      if (peer !== ws) {
        peer.send(message);
      }
    }
  }

  async webSocketClose(ws, code, reason, wasClean) {
    ws.close(code, reason);
    for (const peer of this.ctx.getWebSockets()) {
      peer.send(JSON.stringify({ cmd: "_peer_left" }));
    }
  }

  async webSocketError(ws, error) {
    for (const peer of this.ctx.getWebSockets()) {
      if (peer !== ws) {
        peer.send(JSON.stringify({ cmd: "_peer_left" }));
      }
    }
  }
}