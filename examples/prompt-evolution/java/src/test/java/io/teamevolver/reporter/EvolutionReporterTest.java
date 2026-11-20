package io.teamevolver.reporter;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.junit.jupiter.api.Assertions.*;

class EvolutionReporterTest {
    private final ObjectMapper json = new ObjectMapper();
    private final Map<String, ObjectNode> published = new ConcurrentHashMap<>();
    private final List<String> receivedBodies = new CopyOnWriteArrayList<>();
    private final List<String> receivedKeys = new CopyOnWriteArrayList<>();
    private final List<String> receivedAuth = new CopyOnWriteArrayList<>();
    private HttpServer server;
    private EvolutionReporter.Route task;
    private EvolutionReporter.Route practice;
    private EvolutionReporter reporter;

    @BeforeEach
    void start() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/gateway/sync/skills", exchange -> {
            String profile = query(exchange.getRequestURI().getRawQuery(), "execution_profile_id");
            assertEquals(EvolutionReporter.PLAN_SCHEMA,
                    query(exchange.getRequestURI().getRawQuery(), "execution_capability"));
            assertEquals("fixture-owner", query(exchange.getRequestURI().getRawQuery(), "user_id"));
            assertEquals("Bearer fixture-tenant-token", exchange.getRequestHeaders().getFirst("Authorization"));
            byte[] body = published.get(profile).toString().getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.createContext("/gateway/api/public/traces", exchange -> {
            assertEquals("HTTP/1.1", exchange.getProtocol());
            assertNull(exchange.getRequestHeaders().getFirst("Upgrade"));
            receivedBodies.add(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            receivedKeys.add(exchange.getRequestHeaders().getFirst("Idempotency-Key"));
            receivedAuth.add(exchange.getRequestHeaders().getFirst("Authorization"));
            byte[] response = "{\"id\":\"trace-existing\"}".getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        URI base = URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/gateway");
        task = route(base, "task", "task-agent", "TASK_PRESET", "TASK_LF_PUBLIC", "TASK_LF_SECRET");
        practice = route(base, "practice", "practice-agent", "PRACTICE_PRESET", "PRACTICE_LF_PUBLIC", "PRACTICE_LF_SECRET");
        Map<String, String> secrets = Map.of("TENANT_TOKEN", "fixture-tenant-token", "TASK_LF_PUBLIC", "task-public",
                "TASK_LF_SECRET", "task-secret", "PRACTICE_LF_PUBLIC", "practice-public",
                "PRACTICE_LF_SECRET", "practice-secret");
        reporter = new EvolutionReporter(List.of(task, practice), secrets::get,
                HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NEVER).build(), Duration.ofSeconds(3));
        published.put("task", release(task, "task-release-1", "Rule {{ policy }}"));
        published.put("practice", release(practice, "practice-release-1", "Rule {{ policy }}"));
    }

    @AfterEach
    void stop() { server.stop(0); }

    @Test
    void twoAgentsWithIdenticalTemplatesKeepSeparateProfilesAndCredentials() throws Exception {
        for (EvolutionReporter.Route route : List.of(task, practice)) {
            EvolutionReporter.Release loaded = reporter.pullPublished(route.identity());
            ObjectNode variables = variables("original business rule");
            EvolutionReporter.Observation observation = reporter.observe(invocation(route), loaded, variables,
                    json.createArrayNode(), "question", request(loaded, variables, json.createArrayNode(), "question"));
            reporter.upsert(observation);
        }
        JsonNode first = json.readTree(receivedBodies.get(0));
        JsonNode second = json.readTree(receivedBodies.get(1));
        assertEquals("task", first.at("/metadata/teamevolver_execution/execution_profile_id").asText());
        assertEquals("task-agent", first.at("/metadata/teamevolver_execution/agent_id").asText());
        assertEquals("practice", second.at("/metadata/teamevolver_execution/execution_profile_id").asText());
        assertEquals("practice-agent", second.at("/metadata/teamevolver_execution/agent_id").asText());
        assertNotEquals(receivedAuth.get(0), receivedAuth.get(1));
        assertEquals("actual-customer", first.path("userId").asText());
        assertEquals("real-source-session", first.path("sessionId").asText());
    }

    @Test
    void refusesRouteGuessedFromMatchingTemplateOrLatestPreset() {
        EvolutionReporter.Identity unknownAgent = new EvolutionReporter.Identity("fixture-tenant", "fixture-project",
                "TASK_PRESET", 1, "different-agent", "fixture-owner");
        EvolutionReporter.Identity otherVersion = new EvolutionReporter.Identity("fixture-tenant", "fixture-project",
                "TASK_PRESET", 2, "task-agent", "fixture-owner");
        assertThrows(IllegalArgumentException.class, () -> reporter.route(unknownAgent));
        assertThrows(IllegalArgumentException.class, () -> reporter.route(otherVersion));
    }

    @Test
    void refusesDuplicateRouteConfiguration() {
        assertThrows(IllegalArgumentException.class, () -> new EvolutionReporter(List.of(task, task), key -> "unused",
                HttpClient.newHttpClient(), Duration.ofSeconds(1)));
    }

    @Test
    void cannotAttachAnotherAgentsLoadedRelease() throws Exception {
        EvolutionReporter.Release loaded = reporter.pullPublished(practice.identity());
        assertThrows(IllegalArgumentException.class, () -> reporter.observe(invocation(task), loaded,
                variables("same"), json.createArrayNode(), "question", json.createObjectNode()));
    }

    @Test
    void oldInvocationKeepsItsActuallyLoadedReleaseWhenPublishedHeadChanges() throws Exception {
        EvolutionReporter.Release old = reporter.pullPublished(task.identity());
        published.put("task", release(task, "task-release-2", "Changed {{ policy }}"));
        EvolutionReporter.Release latest = reporter.pullPublished(task.identity());
        ObjectNode variables = variables("literal content");
        EvolutionReporter.Observation original = reporter.observe(invocation(task), old, variables,
                json.createArrayNode(), "question", request(old, variables, json.createArrayNode(), "question"));
        JsonNode metadata = json.readTree(original.body()).at("/metadata/teamevolver_execution");
        assertEquals("task-release-1", metadata.path("release_id").asText());
        assertEquals("task-release-2", latest.releaseId());
        assertThrows(IllegalArgumentException.class, () -> reporter.observe(invocation(task), latest, variables,
                json.createArrayNode(), "question", request(old, variables, json.createArrayNode(), "question")));
    }

    @Test
    void freezesPayloadAndRetriesSameRealTraceAndBytes() throws Exception {
        EvolutionReporter.Release loaded = reporter.pullPublished(task.identity());
        ObjectNode variables = variables("literal");
        EvolutionReporter.Observation observation = reporter.observe(invocation(task), loaded, variables,
                json.createArrayNode(), "question", request(loaded, variables, json.createArrayNode(), "question"));
        variables.put("policy", "mutated after observation");
        loaded.plan().put("schema_version", "mutated clone");
        reporter.upsert(observation);
        reporter.upsert(observation);
        assertEquals(receivedBodies.get(0), receivedBodies.get(1));
        assertEquals(receivedKeys.get(0), receivedKeys.get(1));
        assertEquals("trace-existing", json.readTree(receivedBodies.get(0)).path("id").asText());
        assertEquals("literal", json.readTree(observation.body())
                .at("/metadata/teamevolver_execution/template_inputs/template_variables/policy").asText());
    }

    @Test
    void requiresOriginalVariablesAndPublicHistory() throws Exception {
        EvolutionReporter.Release loaded = reporter.pullPublished(task.identity());
        assertThrows(IllegalArgumentException.class, () -> loaded.renderComponents(json.createObjectNode()));
        ObjectNode extra = variables("literal").put("guessedVariable", "unregistered");
        assertThrows(IllegalArgumentException.class, () -> loaded.renderComponents(extra));
        ArrayNode privileged = json.createArrayNode().add(message("system", "unregistered override"));
        assertThrows(IllegalArgumentException.class, () -> reporter.observe(invocation(task), loaded,
                variables("literal"), privileged, "question", json.createObjectNode()));
    }

    @Test
    void treatsInsertedTemplateMarkersAsLiteralText() throws Exception {
        EvolutionReporter.Release loaded = reporter.pullPublished(task.identity());
        ObjectNode vars = variables("keep {{ policy }} and $1 and \\ here");
        assertEquals("Rule keep {{ policy }} and $1 and \\ here", loaded.renderComponents(vars).get(0).path("content").asText());
    }

    @Test
    void selectsVersionedInlineContextByOriginalValueAndKeepsSourceVariables() throws Exception {
        ObjectNode current = published.get("task");
        ObjectNode plan = (ObjectNode) current.at("/execution/document/execution_plan");
        ObjectNode asset = plan.withArray("components").addObject().put("component_id", "task:policy-value")
                .put("kind", "context_fragment").put("slot", "context").put("order", 1).put("revision", "2")
                .put("content", "improved {{literal}} content").put("content_hash", sha("improved {{literal}} content"));
        asset.putObject("binding").putObject("inline_variable").put("component_id", "task:agent-prompt")
                .put("name", "policy").put("source_value_hash", sha("original source value"));
        EvolutionReporter.Release loaded = reporter.pullPublished(task.identity());
        ObjectNode vars = variables("original source value");
        assertEquals(1, loaded.renderComponents(vars).size());
        assertEquals("Rule improved {{literal}} content", loaded.renderComponents(vars).get(0).path("content").asText());
        assertEquals("Rule another source value", loaded.renderComponents(variables("another source value"))
                .get(0).path("content").asText());
        JsonNode metadata = json.readTree(reporter.observe(invocation(task), loaded, vars, json.createArrayNode(),
                "question", request(loaded, vars, json.createArrayNode(), "question")).body());
        assertEquals("original source value", metadata.at(
                "/metadata/teamevolver_execution/template_inputs/template_variables/policy").asText());
    }

    @Test
    void rejectsWrongModelParametersAndModifiedObservedMessages() throws Exception {
        EvolutionReporter.Release loaded = reporter.pullPublished(task.identity());
        ObjectNode vars = variables("literal");
        ObjectNode wrongModel = request(loaded, vars, json.createArrayNode(), "question").put("model", "other-model");
        assertThrows(IllegalArgumentException.class, () -> reporter.observe(invocation(task), loaded, vars,
                json.createArrayNode(), "question", wrongModel));
        ObjectNode altered = request(loaded, vars, json.createArrayNode(), "question");
        ((ObjectNode) altered.withArray("messages").get(0)).put("content", "different business rule");
        assertThrows(IllegalArgumentException.class, () -> reporter.observe(invocation(task), loaded, vars,
                json.createArrayNode(), "question", altered));
    }

    @Test
    void preservesRenderedUserTemplateAndHistoryInTheirRealRoles() throws Exception {
        EvolutionReporter.Release loaded = reporter.pullPublished(task.identity());
        ArrayNode history = json.createArrayNode().add(message("user", "previous question"))
                .add(message("assistant", "previous response"));
        String current = "Conversation history\n[real business input]\nPlease judge all tasks.";
        ObjectNode vars = variables("literal");
        JsonNode payload = json.readTree(reporter.observe(invocation(task), loaded, vars, history, current,
                request(loaded, vars, history, current)).body());
        assertEquals(history, payload.at("/metadata/teamevolver_execution/template_inputs/input_messages"));
        assertEquals(1, payload.at("/metadata/teamevolver_execution/template_inputs/template_refs").size());
    }

    @Test
    void rejectsWrongReturnedBindingAndComponentHash() {
        ObjectNode current = published.get("task");
        ((ObjectNode) current.at("/execution/document/execution_plan/binding")).put("agent_id", "practice-agent");
        assertThrows(IllegalArgumentException.class, () -> reporter.pullPublished(task.identity()));
        current = release(task, "release", "Rule {{policy}}");
        ((ObjectNode) current.at("/execution/document/execution_plan/components/0")).put("content", "silently changed");
        published.put("task", current);
        assertThrows(IllegalArgumentException.class, () -> reporter.pullPublished(task.identity()));
    }

    @Test
    void rejectsAnotherTenantsSameNamedProfileAgentAndOwner() {
        published.get("task").put("tenant_id", "another-tenant-with-identical-ids");
        IllegalArgumentException error = assertThrows(IllegalArgumentException.class,
                () -> reporter.pullPublished(task.identity()));
        assertTrue(error.getMessage().contains("tenant identity"));
        published.get("task").remove("tenant_id");
        assertThrows(IllegalArgumentException.class, () -> reporter.pullPublished(task.identity()));
    }

    @Test
    void acceptsRegisteredFixedPromptWithoutInventingVariableContract() throws Exception {
        ObjectNode fixed = release(task, "fixed-release", "Fixed business rule");
        ((ObjectNode) fixed.at("/execution/document/execution_plan/environment")).remove("template_input_contract");
        published.put("task", fixed);
        EvolutionReporter.Release loaded = reporter.pullPublished(task.identity());
        ObjectNode vars = json.createObjectNode();
        JsonNode body = json.readTree(reporter.observe(invocation(task), loaded, vars, json.createArrayNode(),
                "question", request(loaded, vars, json.createArrayNode(), "question")).body());
        assertFalse(body.at("/metadata/teamevolver_execution").has("template_inputs"));
        assertThrows(IllegalArgumentException.class, () -> loaded.renderComponents(variables("unregistered value")));
    }

    @Test
    void preservesTEFiniteScalarRenderingAndRejectsNonFiniteValues() throws Exception {
        EvolutionReporter.Release loaded = reporter.pullPublished(task.identity());
        assertEquals("Rule true", loaded.renderComponents(json.createObjectNode().put("policy", true))
                .get(0).path("content").asText());
        assertEquals("Rule 42", loaded.renderComponents(json.createObjectNode().put("policy", 42))
                .get(0).path("content").asText());
        double[] numbers = {1e-6, 0.0001, 1e16, -0.0, 0.0, 1e23, 1.25, -1e-7};
        String[] pythonJSON = {"1e-06", "0.0001", "1e+16", "-0.0", "0.0", "1e+23", "1.25", "-1e-07"};
        for (int i = 0; i < numbers.length; i++) {
            assertEquals("Rule " + pythonJSON[i], loaded.renderComponents(json.createObjectNode().put("policy", numbers[i]))
                    .get(0).path("content").asText());
        }
        assertThrows(IllegalArgumentException.class,
                () -> loaded.renderComponents(json.createObjectNode().put("policy", Double.NaN)));
        assertThrows(IllegalArgumentException.class,
                () -> loaded.renderComponents(json.createObjectNode().put("policy", Double.POSITIVE_INFINITY)));
    }

    @Test
    void propertiesKeepOwnersAndRoutesExplicit() {
        Properties properties = new Properties();
        properties.setProperty("routes", "task,practice");
        for (EvolutionReporter.Route route : List.of(task, practice)) {
            String prefix = "route." + route.profileId() + ".";
            Map<String, String> fields = Map.ofEntries(Map.entry("tenant", route.identity().tenantId()),
                    Map.entry("project", route.identity().projectId()), Map.entry("presetCode", route.identity().presetCode()),
                    Map.entry("presetVersion", "1"), Map.entry("agentId", route.identity().agentId()),
                    Map.entry("executionUserId", route.identity().executionUserId()), Map.entry("profileId", route.profileId()),
                    Map.entry("runtimeType", route.runtimeType()), Map.entry("traceName", route.traceName()),
                    Map.entry("teBase", route.teBase().toString()), Map.entry("tenantTokenEnv", route.tenantTokenEnv()),
                    Map.entry("langfuseBase", route.langfuseBase().toString()),
                    Map.entry("langfusePublicKeyEnv", route.langfusePublicKeyEnv()),
                    Map.entry("langfuseSecretKeyEnv", route.langfuseSecretKeyEnv()));
            fields.forEach((field, value) -> properties.setProperty(prefix + field, value));
        }
        EvolutionReporter configured = EvolutionReporter.fromProperties(properties, key -> "not-used");
        assertEquals(task, configured.route(task.identity()));
        assertEquals(practice, configured.route(practice.identity()));
    }

    private EvolutionReporter.Route route(URI base, String profile, String agent, String code,
                                           String publicKeyEnv, String secretKeyEnv) {
        return new EvolutionReporter.Route(new EvolutionReporter.Identity("fixture-tenant", "fixture-project", code,
                1, agent, "fixture-owner"), profile, "observed_http", "invoke_agent " + agent,
                base, "TENANT_TOKEN", base, publicKeyEnv, secretKeyEnv);
    }

    private EvolutionReporter.Invocation invocation(EvolutionReporter.Route route) {
        return new EvolutionReporter.Invocation(route.identity(), "trace-existing", "real-source-session", "actual-customer");
    }

    private ObjectNode variables(String value) { return json.createObjectNode().put("policy", value); }

    private ObjectNode message(String role, String content) {
        return json.createObjectNode().put("role", role).put("content", content);
    }

    private ObjectNode request(EvolutionReporter.Release loaded, ObjectNode variables, ArrayNode history, String user) {
        ObjectNode result = (ObjectNode) loaded.plan().path("environment").path("request");
        ArrayNode messages = loaded.renderComponents(variables);
        messages.addAll(history.deepCopy()).add(message("user", user));
        result.set("messages", messages);
        return result;
    }

    private ObjectNode release(EvolutionReporter.Route route, String releaseId, String template) {
        ObjectNode plan = json.createObjectNode().put("schema_version", EvolutionReporter.PLAN_SCHEMA);
        plan.putObject("binding").put("profile_id", route.profileId()).put("runtime_type", route.runtimeType())
                .put("agent_id", route.identity().agentId()).put("user_id", route.identity().executionUserId());
        ObjectNode environment = plan.putObject("environment");
        environment.putObject("request").put("model", "fixture-model").put("temperature", 0);
        environment.putObject("template_input_contract").put("schema_version", "teamevolver.template-input-contract.v1")
                .putArray("variable_names").add("policy");
        String componentId = route.profileId() + ":agent-prompt";
        ObjectNode part = plan.putArray("components").addObject().put("component_id", componentId)
                .put("kind", "agent_prompt").put("slot", "system").put("order", 0).put("revision", "1")
                .put("content", template).put("content_hash", sha(template));
        part.putObject("binding").put("template_id", componentId);
        ObjectNode result = json.createObjectNode().put("status", "ok").put("tenant_id", route.identity().tenantId());
        ObjectNode execution = result.putObject("execution").put("release_id", releaseId)
                .put("composition_hash", sha("fixture:" + releaseId)).put("profile_id", route.profileId());
        ObjectNode document = execution.putObject("document");
        document.set("execution_plan", plan);
        document.putArray("skill_dependencies");
        return result;
    }

    private static String query(String raw, String name) {
        for (String item : raw.split("&")) {
            String[] pair = item.split("=", 2);
            if (URLDecoder.decode(pair[0], StandardCharsets.UTF_8).equals(name)) {
                return URLDecoder.decode(pair[1], StandardCharsets.UTF_8);
            }
        }
        return "";
    }

    private static String sha(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) { throw new IllegalStateException(exception); }
    }
}
