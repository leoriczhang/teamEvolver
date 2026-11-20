package io.teamevolver.reporter;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;

/** Read existing requests, pull the real released composition, and compare metadata without sending it. */
public final class RecordedRequestCheck {
    private RecordedRequestCheck() { }

    public static void main(String[] args) throws Exception {
        if (args.length != 3) {
            throw new IllegalArgumentException("Usage: RecordedRequestCheck routes.properties samples.json report.json");
        }
        Properties properties = new Properties();
        try (var reader = Files.newBufferedReader(Path.of(args[0]), StandardCharsets.UTF_8)) {
            properties.load(reader);
        }
        EvolutionReporter reporter = EvolutionReporter.fromProperties(properties, System::getenv);
        ObjectMapper json = new ObjectMapper();
        JsonNode samples = json.readTree(Files.readString(Path.of(args[1]), StandardCharsets.UTF_8));
        if (!samples.isArray() || samples.isEmpty()) throw new IllegalArgumentException("Recorded samples are required");
        ArrayNode results = json.createArrayNode();
        for (JsonNode sample : samples) {
            JsonNode id = sample.path("identity");
            var identity = new EvolutionReporter.Identity(id.path("tenant_id").asText(), id.path("project_id").asText(),
                    id.path("preset_code").asText(), id.path("preset_version").asInt(),
                    id.path("agent_id").asText(), id.path("execution_user_id").asText());
            EvolutionReporter.Release published = reporter.pullPublished(identity);
            // This entry point checks already-recorded inputs against their known release.
            // The business hook must keep its own actually loaded Release for each live invocation.
            if (!published.releaseId().equals(sample.path("expected_observation").path("release_id").asText())) {
                throw new IllegalArgumentException("Recorded request requires a historical released snapshot");
            }
            var invocation = new EvolutionReporter.Invocation(identity, sample.path("trace_id").asText(),
                    sample.path("session_id").asText(), sample.path("customer_user_id").asText());
            var observation = reporter.observe(invocation, published, (ObjectNode) sample.path("actual_variables"),
                    (ArrayNode) sample.path("public_history"), sample.path("current_user_message").asText(),
                    (ObjectNode) sample.path("final_model_request"));
            JsonNode body = json.readTree(observation.body());
            JsonNode generated = body.path("metadata").path("teamevolver_execution");
            if (!generated.equals(sample.path("expected_observation"))) {
                throw new IllegalArgumentException("Reporter metadata differs from the recorded Langfuse observation");
            }
            ObjectNode result = results.addObject().put("profile_id", published.route().profileId())
                    .put("tenant_id", identity.tenantId()).put("agent_id", identity.agentId())
                    .put("trace_id", invocation.traceId()).put("release_id", published.releaseId())
                    .put("tenant_verified", true).put("actual_request_accepted", true)
                    .put("metadata_matches_recorded_langfuse", true).put("sent_to_langfuse", false)
                    .put("reexecuted_model", false);
            result.set("generated_metadata", generated);
            result.put("fixture_scope", sample.path("fixture_scope").asText());
        }
        Files.writeString(Path.of(args[2]), json.writerWithDefaultPrettyPrinter().writeValueAsString(results),
                StandardCharsets.UTF_8);
        System.out.println("Validated " + results.size() + " recorded requests against real TE released compositions.");
    }
}
