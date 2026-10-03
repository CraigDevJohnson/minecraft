import java.net.InetAddress;
import java.nio.ByteBuffer;
import java.security.SecureRandom;
import java.util.Arrays;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import tel.schich.libdatachannel.*;

/** Isolated bundled-library UDP/DTLS/SCTP smoke test; does not authenticate to Geyser. */
public final class NetherNetNativeProbe {
    private static final long DEADLINE = System.nanoTime() + TimeUnit.SECONDS.toNanos(25);

    private static void await(CompletableFuture<?> result) throws Exception {
        result.get(Math.max(1, DEADLINE - System.nanoTime()), TimeUnit.NANOSECONDS);
    }

    private static CompletableFuture<Void> gathering(PeerConnection peer) {
        var done = new CompletableFuture<Void>();
        peer.onGatheringStateChange.register((connection, state) -> {
            if (state == GatheringState.RTC_GATHERING_COMPLETE) done.complete(null);
        });
        return done;
    }

    private static ByteBuffer buffer(byte[] bytes) {
        return ByteBuffer.allocateDirect(bytes.length).put(bytes).flip();
    }

    private static void probe(int port) throws Exception {
        if (!System.getProperty("os.name").equals("Linux")
                || !java.util.Set.of("aarch64", "arm64").contains(System.getProperty("os.arch"))) {
            throw new IllegalStateException("Linux ARM64 is required");
        }
        LibDataChannel.setLogLevel(LibDataChannel.LogLevel.NONE);
        LibDataChannelArchDetect.initialize();
        var config = PeerConnectionConfiguration.DEFAULT.withDisableAutoNegotiation(true)
                .withEnableIceTcp(false).withBindAddress(InetAddress.getByName("127.0.0.1"));
        try (var server = PeerConnection.createPeer(config.withEnableIceUdpMux(true)
                     .withPortRangeBegin(port).withPortRangeEnd(port));
             var client = PeerConnection.createPeer(config)) {
            byte[] nonce = new byte[32];
            new SecureRandom().nextBytes(nonce);
            var received = new CompletableFuture<Void>();
            server.onDataChannel.register((peer, channel) -> channel.onMessage.register(
                    DataChannelCallback.Message.handleBinary((incoming, bytes) -> {
                        byte[] actual = new byte[bytes.remaining()];
                        bytes.get(actual);
                        if (Arrays.equals(actual, nonce)) incoming.sendMessage(buffer(actual));
                        else received.completeExceptionally(new IllegalStateException("unexpected payload"));
                    })));
            var reliable = client.createDataChannel("ReliableDataChannel");
            var unreliable = client.createDataChannel("UnreliableDataChannel",
                    DataChannelInitSettings.DEFAULT.withReliability(DataChannelReliability.DEFAULT
                            .withUnordered(true).withUnreliable(true).withMaxRetransmits(0)));
            var reliableEcho = new CompletableFuture<Void>();
            var unreliableEcho = new CompletableFuture<Void>();
            for (var channel : new DataChannel[]{reliable, unreliable}) {
                var echo = channel == reliable ? reliableEcho : unreliableEcho;
                channel.onMessage.register(DataChannelCallback.Message.handleBinary((incoming, bytes) -> {
                    byte[] actual = new byte[bytes.remaining()];
                    bytes.get(actual);
                    if (Arrays.equals(actual, nonce)) echo.complete(null);
                    else echo.completeExceptionally(new IllegalStateException("unexpected echo"));
                }));
                channel.onOpen.register(open -> open.sendMessage(buffer(nonce)));
            }
            var clientGathered = gathering(client);
            var serverGathered = gathering(server);
            client.setLocalDescription("offer");
            await(clientGathered);
            server.setRemoteDescription(client.localDescription(), SessionDescriptionType.OFFER);
            server.setLocalDescription("answer");
            await(serverGathered);
            client.setRemoteDescription(server.localDescription(), SessionDescriptionType.ANSWER);
            await(CompletableFuture.anyOf(received, CompletableFuture.allOf(reliableEcho, unreliableEcho)));
            var pair = server.selectedCandidatePair();
            if (!"udp".equals(pair.localTransport()) || !"udp".equals(pair.remoteTransport())
                    || pair.local() == null || pair.local().getPort() != port) {
                throw new IllegalStateException("unexpected selected UDP port");
            }
        }
        System.out.println("{\"status\":\"passed\",\"scope\":\"isolated_bundled_native_transport\","
                + "\"udp_port\":" + port + ",\"reliable_echo\":true,\"unreliable_echo\":true,"
                + "\"geyser_admission_verified\":false,\"bedrock_join_verified\":false}");
    }

    public static void main(String[] args) {
        var timeout = Executors.newSingleThreadScheduledExecutor(task -> {
            var thread = new Thread(task, "native-probe-timeout");
            thread.setDaemon(true);
            return thread;
        });
        timeout.schedule(() -> Runtime.getRuntime().halt(124), 30, TimeUnit.SECONDS);
        int result = 0;
        try {
            int port = args.length == 0 ? 19133 : Integer.parseInt(args[0]);
            if (args.length > 1 || port < 1 || port > 65535) throw new IllegalArgumentException();
            probe(port);
        } catch (Throwable failure) {
            System.out.println("{\"status\":\"failed\",\"scope\":\"isolated_bundled_native_transport\"}");
            result = 1;
        } finally {
            timeout.shutdownNow();
        }
        System.exit(result);
    }
}
