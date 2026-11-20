package io.teamevolver.reporter;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.fasterxml.jackson.core.io.schubfach.DoubleToDecimal;

import java.io.IOException;
import java.math.BigDecimal;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.function.Function;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Explicit, per-invocation attribution for existing business model transports. */
public final class EvolutionReporter {
    public static final String PLAN_SCHEMA = "teamevolver.execution-plan.v1";
    private static final String INPUT_SCHEMA = "teamevolver.execution-inputs.v1";
    private static final Pattern PLACEHOLDER = Pattern.compile("\\{\\{\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*}}");
    private static final ObjectMapper JSON = new ObjectMapper();

    /** executionUserId is the configured TE AgentPrincipal, separate from the customer userId. */
    public record Identity(String tenantId, String projectId, String presetCode, int presetVersion,
                           String agentId, String executionUserId) {
        public Identity {
            required(tenantId, "tenantId");
            required(projectId, "projectId");
            required(presetCode, "presetCode");
            require(presetVersion > 0, "presetVersion must be positive");
            required(agentId, "agentId");
            required(executionUserId, "executionUserId");
        }
    }

    /** Every route has its own project credentials and an explicit preset version. */
    public record Route(Identity identity, String profileId, String runtimeType, String traceName,
                        URI teBase, String tenantTokenEnv, URI langfuseBase,
                        String langfusePublicKeyEnv, String langfuseSecretKeyEnv) {
        public Route {
            require(identity != null, "route identity is required");
            required(profileId, "profileId");
            required(runtimeType, "runtimeType");
            required(traceName, "traceName");
            baseUri(teBase);
            baseUri(langfuseBase);
            required(tenantTokenEnv, "tenantTokenEnv");
            required(langfusePublicKeyEnv, "langfusePublicKeyEnv");
            required(langfuseSecretKeyEnv, "langfuseSecretKeyEnv");
        }
    }

    /** Carries the original model interaction across synchronous, async and streaming paths. */
    public record Invocation(Identity identity, String traceId, String sessionId, String customerUserId) {
        public Invocation {
            require(identity != null, "invocation identity is required");
            required(traceId, "actual traceId");
            required(sessionId, "actual sessionId");
            required(customerUserId, "actual customerUserId");
        }
    }

    /** Caller must install/use this frozen release before describing it as loaded. */
    public static final class Release {
        private final Route route;
        private final String releaseId;
        private final String compositionHash;
        private final ObjectNode plan;

        private Release(Route route, String releaseId, String compositionHash, ObjectNode plan) {
            this.route = route;
            this.releaseId = releaseId;
            this.compositionHash = compositionHash;
            this.plan = plan.deepCopy();
        }

        public Route route() { return route; }
        public String releaseId() { return releaseId; }
        public String compositionHash() { return compositionHash; }
        public ObjectNode plan() { return plan.deepCopy(); }

        /** Produces the registered privileged messages using actual per-request variables. */
        public ArrayNode renderComponents(ObjectNode variables) {
            validateVariables(plan, variables);
            List<JsonNode> components = new ArrayList<>();
            plan.withArray("components").forEach(components::add);
            components.sort(java.util.Comparator.comparingInt(part -> part.path("order").asInt()));
            Map<String, ObjectNode> overrides = inlineOverrides(components, variables);
            ArrayNode messages = JSON.createArrayNode();
            for (JsonNode part : components) {
                if (part.path("binding").has("inline_variable")) continue;
                String slot = part.path("slot").asText();
                require(Set.of("system", "developer", "context").contains(slot), "unsupported component slot");
                String role = slot.equals("context") ? "developer" : slot;
                ObjectNode effective = variables.deepCopy();
                ObjectNode selected = overrides.get(part.path("component_id").asText());
                if (selected != null) effective.setAll(selected);
                messages.add(message(role, render(part.path("content").asText(), effective)));
            }
            return messages;
        }
    }

    /** Immutable payload. Persist the same body/key in the business outbox before retrying. */
    public static final class Observation {
        private final Route route;
        private final String body;
        private final String deduplicationKey;

        private Observation(Route route, String body) {
            this.route = route;
            this.body = body;
            this.deduplicationKey = sha256(route.identity().tenantId() + "\n"
                    + route.identity().projectId() + "\n" + body);
        }

        public String body() { return body; }
        public String deduplicationKey() { return deduplicationKey; }
    }

    private final List<Route> routes;
    private final Function<String, String> secrets;
    private final HttpClient http;
    private final Duration timeout;

    public EvolutionReporter(List<Route> routes, Function<String, String> secrets,
                             HttpClient http, Duration timeout) {
        require(routes != null && !routes.isEmpty(), "at least one route is required");
        require(secrets != null && http != null, "secrets and HTTP client are required");
        require(http.followRedirects() == HttpClient.Redirect.NEVER, "HTTP redirects must be disabled");
        require(timeout != null && !timeout.isNegative() && !timeout.isZero(), "timeout must be positive");
        Set<Identity> identities = new HashSet<>();
        for (Route route : routes) {
            require(identities.add(route.identity()), "duplicate invocation route");
        }
        this.routes = List.copyOf(routes);
        this.secrets = secrets;
        this.http = http;
        this.timeout = timeout;
    }

    public static EvolutionReporter fromProperties(Properties properties, Function<String, String> secrets) {
        List<Route> routes = new ArrayList<>();
        for (String name : property(properties, "routes").split(",")) {
            String prefix = "route." + name.trim() + ".";
            Identity identity = new Identity(property(properties, prefix + "tenant"),
                    property(properties, prefix + "project"), property(properties, prefix + "presetCode"),
                    Integer.parseInt(property(properties, prefix + "presetVersion")),
                    property(properties, prefix + "agentId"), property(properties, prefix + "executionUserId"));
            routes.add(new Route(identity, property(properties, prefix + "profileId"),
                    property(properties, prefix + "runtimeType"), property(properties, prefix + "traceName"),
                    URI.create(property(properties, prefix + "teBase")), property(properties, prefix + "tenantTokenEnv"),
                    URI.create(property(properties, prefix + "langfuseBase")),
                    property(properties, prefix + "langfusePublicKeyEnv"),
                    property(properties, prefix + "langfuseSecretKeyEnv")));
        }
        return new EvolutionReporter(routes, secrets,
                HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NEVER).build(),
                Duration.ofSeconds(Long.parseLong(properties.getProperty("timeoutSeconds", "15"))));
    }

    public Route route(Identity actual) {
        return routes.stream().filter(route -> route.identity().equals(actual)).findFirst()
                .orElseThrow(() -> new IllegalArgumentException("no exact business invocation route"));
    }

    /** Read a published composition. The actual business loader decides when to activate it. */
    public Release pullPublished(Identity identity) throws IOException, InterruptedException {
        Route route = route(identity);
        String query = "/sync/skills?user_id=" + encode(identity.executionUserId())
                + "&execution_profile_id=" + encode(route.profileId())
                + "&execution_capability=" + encode(PLAN_SCHEMA);
        HttpRequest request = HttpRequest.newBuilder(endpoint(route.teBase(), query)).timeout(timeout)
                .header("Authorization", "Bearer " + secret(route.tenantTokenEnv()))
                .GET().build();
        JsonNode result = sendJson(request, "TE pull");
        require(result.path("status").asText().equals("ok"), "TE pull did not return a registry result");
        require(result.path("tenant_id").asText().equals(identity.tenantId()),
                "TE pull tenant identity is missing or differs from the invocation route");
        JsonNode execution = result.path("execution");
        require(execution.path("profile_id").asText().equals(route.profileId()), "TE profile binding mismatch");
        String releaseId = required(execution.path("release_id").asText(), "released composition ID");
        String hash = execution.path("composition_hash").asText();
        require(hash.matches("[0-9a-f]{64}"), "invalid released composition hash");
        JsonNode rawPlan = execution.path("document").path("execution_plan");
        require(rawPlan.isObject() && rawPlan.path("schema_version").asText().equals(PLAN_SCHEMA),
                "TE execution plan is unavailable");
        ObjectNode plan = (ObjectNode) rawPlan;
        JsonNode binding = plan.path("binding");
        require(binding.path("profile_id").asText().equals(route.profileId())
                        && binding.path("agent_id").asText().equals(identity.agentId())
                        && binding.path("user_id").asText().equals(identity.executionUserId())
                        && binding.path("runtime_type").asText().equals(route.runtimeType()),
                "TE execution plan belongs to a different Agent, owner or runtime");
        require(execution.path("document").path("skill_dependencies").isArray()
                        && execution.path("document").path("skill_dependencies").isEmpty(),
                "this Prompt-only loader requires an execution loader for Skill dependencies");
        require(plan.path("components").isArray() && !plan.path("components").isEmpty(),
                "TE execution plan has no components");
        Set<String> componentIds = new HashSet<>();
        Set<Integer> orders = new HashSet<>();
        for (JsonNode part : plan.path("components")) {
            require(componentIds.add(required(part.path("component_id").asText(), "component ID")),
                    "duplicate component ID");
            require(part.path("order").isIntegralNumber() && part.path("order").asInt() >= 0
                    && orders.add(part.path("order").asInt()), "invalid component order");
            require(Set.of("agent_prompt", "context_fragment").contains(part.path("kind").asText()),
                    "unsupported component kind");
            require(sha256(required(part.path("content").asText(), "component content"))
                    .equals(part.path("content_hash").asText()), "component content hash mismatch");
            required(part.path("revision").asText(), "component revision");
        }
        JsonNode contract = plan.path("environment").get("template_input_contract");
        require(contract == null || contract.path("schema_version").asText()
                .equals("teamevolver.template-input-contract.v1"), "unsupported shared-template input contract");
        return new Release(route, releaseId, hash, plan);
    }

    /** Called at the real final model transport with the original input values and request. */
    public Observation observe(Invocation invocation, Release actuallyLoaded, ObjectNode actualVariables,
                               ArrayNode publicHistory, String currentUserMessage, ObjectNode finalModelRequest) {
        Route route = route(invocation.identity());
        require(actuallyLoaded != null && route.equals(actuallyLoaded.route), "loaded release route mismatch");
        required(currentUserMessage, "actual current user message");
        validateHistory(publicHistory);
        ArrayNode expected = actuallyLoaded.renderComponents(actualVariables);
        expected.addAll(publicHistory.deepCopy());
        String prefix = actuallyLoaded.plan.path("environment").path("message_prefixes").path("user").asText("");
        expected.add(message("user", prefixed(currentUserMessage, prefix)));
        require(finalModelRequest != null && expected.equals(finalModelRequest.get("messages")),
                "actual model messages differ from the loaded release and supplied inputs");
        JsonNode frozenRequest = actuallyLoaded.plan.path("environment").get("request");
        require(frozenRequest != null && frozenRequest.isObject(), "release has no frozen model request");
        ObjectNode parameters = finalModelRequest.deepCopy();
        parameters.remove("messages");
        require(frozenRequest.equals(parameters), "actual model parameters differ from the loaded release");

        ObjectNode refs = JSON.createObjectNode();
        for (JsonNode part : actuallyLoaded.plan.path("components")) {
            if (part.path("kind").asText().equals("agent_prompt")) {
                ObjectNode ref = refs.putObject(part.path("component_id").asText());
                ref.put("template_id", part.path("binding").path("template_id")
                        .asText(part.path("component_id").asText()));
                ref.put("revision", part.path("revision").asText());
                ref.put("content_hash", part.path("content_hash").asText());
            }
        }
        require(!refs.isEmpty(), "loaded release has no registered Prompt");
        ObjectNode inputs = JSON.createObjectNode();
        inputs.put("schema_version", INPUT_SCHEMA);
        inputs.set("template_refs", refs);
        inputs.set("template_variables", actualVariables.deepCopy());
        inputs.set("input_messages", publicHistory.deepCopy());
        ObjectNode observation = JSON.createObjectNode();
        observation.put("schema_version", "teamevolver.execution-observation.v1");
        observation.put("execution_profile_id", route.profileId());
        observation.put("agent_id", invocation.identity().agentId());
        observation.put("runtime_type", route.runtimeType());
        observation.put("release_id", actuallyLoaded.releaseId);
        observation.put("composition_hash", actuallyLoaded.compositionHash);
        if (actuallyLoaded.plan.path("environment").has("template_input_contract")) {
            observation.set("template_inputs", inputs);
        }
        ObjectNode trace = JSON.createObjectNode();
        trace.put("id", invocation.traceId());
        // Preserve the upstream session/user fields. The runtime owner is separate.
        trace.put("sessionId", invocation.sessionId());
        trace.put("userId", invocation.customerUserId());
        trace.put("name", route.traceName());
        trace.putObject("metadata").set("teamevolver_execution", observation);
        return new Observation(route, trace.toString());
    }

    /** The caller schedules retries or writes its existing durable outbox as appropriate. */
    public void upsert(Observation observation) throws IOException, InterruptedException {
        require(observation != null, "observation is required");
        Route route = observation.route;
        require(routes.contains(route), "observation belongs to another reporter");
        String auth = secret(route.langfusePublicKeyEnv()) + ":" + secret(route.langfuseSecretKeyEnv());
        HttpRequest request = HttpRequest.newBuilder(endpoint(route.langfuseBase(), "/api/public/traces"))
                .version(HttpClient.Version.HTTP_1_1).timeout(timeout).header("Content-Type", "application/json")
                .header("Authorization", "Basic " + Base64.getEncoder().encodeToString(auth.getBytes(StandardCharsets.UTF_8)))
                .header("Idempotency-Key", observation.deduplicationKey)
                .POST(HttpRequest.BodyPublishers.ofString(observation.body, StandardCharsets.UTF_8)).build();
        JsonNode result = sendJson(request, "Langfuse metadata upsert");
        require(result.path("id").asText().equals(JSON.readTree(observation.body).path("id").asText()),
                "Langfuse metadata upsert did not acknowledge the original Trace ID");
    }

    private JsonNode sendJson(HttpRequest request, String operation) throws IOException, InterruptedException {
        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new IOException(operation + " failed with HTTP " + response.statusCode());
        }
        if (response.body().length() > 8_000_000) throw new IOException(operation + " response is too large");
        try {
            JsonNode result = JSON.readTree(response.body());
            if (result == null || !result.isObject()) throw new IOException(operation + " returned invalid JSON");
            return result;
        } catch (com.fasterxml.jackson.core.JsonProcessingException exception) {
            // Raw responses can contain credentials or dialogue. Keep the public exception bounded.
            throw new IOException(operation + " returned invalid JSON");
        }
    }

    private String secret(String environmentName) {
        String value = secrets.apply(environmentName);
        require(value != null && !value.isBlank(), "required credential environment is unavailable");
        return value;
    }

    private static void validateVariables(ObjectNode plan, ObjectNode actual) {
        require(actual != null, "actual template variables are required");
        actual.elements().forEachRemaining(value -> require(value.isTextual() || value.isBoolean() || value.isNumber(),
                "template variables must be scalar values"));
        actual.elements().forEachRemaining(value -> require(!value.isFloatingPointNumber()
                || Double.isFinite(value.doubleValue()), "template variables must contain finite numbers"));
        if (!plan.path("environment").has("template_input_contract")) {
            JsonNode defaults = plan.path("environment").path("template_variables");
            require(defaults.isMissingNode() ? actual.isEmpty() : defaults.equals(actual),
                    "fixed-profile variables must equal the registered frozen defaults");
            return;
        }
        JsonNode names = plan.path("environment").path("template_input_contract").path("variable_names");
        require(names.isArray() && !names.isEmpty(), "declared template variables are required");
        Set<String> expected = new HashSet<>();
        for (JsonNode name : names) {
            require(name.isTextual() && name.asText().matches("[A-Za-z_][A-Za-z0-9_]{0,127}")
                    && expected.add(name.asText()), "invalid declared template variable");
        }
        Set<String> supplied = new HashSet<>();
        actual.fieldNames().forEachRemaining(supplied::add);
        require(expected.equals(supplied), "supply exactly the registered variables from the actual request");
    }

    private static Map<String, ObjectNode> inlineOverrides(List<JsonNode> components, ObjectNode variables) {
        Map<String, JsonNode> hosts = new HashMap<>();
        components.forEach(part -> hosts.put(part.path("component_id").asText(), part));
        Map<String, ObjectNode> overrides = new HashMap<>();
        Set<List<String>> selectors = new HashSet<>();
        for (JsonNode part : components) {
            JsonNode binding = part.path("binding");
            if (!binding.has("inline_variable")) continue;
            JsonNode inline = binding.get("inline_variable");
            String hostId = inline.path("component_id").asText();
            String name = inline.path("name").asText();
            String sourceHash = inline.path("source_value_hash").asText();
            JsonNode host = hosts.get(hostId);
            require(part.path("kind").asText().equals("context_fragment") && !binding.has("context_ref")
                    && inline.isObject() && inline.size() == 3 && sourceHash.matches("[0-9a-f]{64}")
                    && name.matches("[A-Za-z_][A-Za-z0-9_]{0,127}")
                    && host != null && host.path("kind").asText().equals("agent_prompt"),
                    "invalid inline Context binding");
            Matcher placeholder = PLACEHOLDER.matcher(host.path("content").asText());
            boolean present = false;
            while (placeholder.find()) present |= placeholder.group(1).equals(name);
            require(present && selectors.add(List.of(hostId, name, sourceHash)),
                    "inline Context must select a unique host variable and source value");
            JsonNode value = variables.get(name);
            if (value != null && value.isTextual() && !value.asText().isEmpty()
                    && sha256(value.asText()).equals(sourceHash)) {
                overrides.computeIfAbsent(hostId, ignored -> JSON.createObjectNode())
                        .put(name, part.path("content").asText());
            }
        }
        return overrides;
    }

    private static void validateHistory(ArrayNode history) {
        require(history != null && history.size() <= 128, "public history must contain at most 128 messages");
        for (JsonNode message : history) {
            require(message.isObject() && message.size() == 2
                    && Set.of("user", "assistant").contains(message.path("role").asText())
                    && message.path("content").isTextual() && !message.path("content").asText().isBlank(),
                    "public history must contain original user/assistant text");
        }
    }

    private static String render(String template, ObjectNode variables) {
        require(!template.contains("{%") && !template.contains("{#"), "unsupported template expressions");
        Matcher matcher = PLACEHOLDER.matcher(template);
        StringBuffer result = new StringBuffer();
        int cursor = 0;
        while (matcher.find()) {
            require(!template.substring(cursor, matcher.start()).contains("{{"), "unsupported template expressions");
            JsonNode value = variables.get(matcher.group(1));
            require(value != null, "actual template variable is missing");
            matcher.appendReplacement(result, Matcher.quoteReplacement(scalarText(value)));
            cursor = matcher.end();
        }
        require(!template.substring(cursor).contains("{{"), "unsupported template expressions");
        matcher.appendTail(result);
        return result.toString();
    }

    private static String scalarText(JsonNode value) {
        if (value.isTextual()) return value.asText();
        if (!value.isFloatingPointNumber()) return value.toString();
        // Jackson supplies the shortest IEEE decimal. Apply TE/Python's JSON
        // presentation thresholds without changing its significant digits.
        double number = Double.parseDouble(value.asText());
        require(Double.isFinite(number), "template variables must contain finite numbers");
        if (number == 0.0) return Double.doubleToRawLongBits(number) < 0 ? "-0.0" : "0.0";
        BigDecimal decimal = new BigDecimal(DoubleToDecimal.toString(number)).stripTrailingZeros();
        int exponent = decimal.precision() - decimal.scale() - 1;
        if (exponent >= 16 || exponent < -4) {
            String coefficient = decimal.movePointLeft(exponent).toPlainString();
            int absolute = Math.abs(exponent);
            return coefficient + "e" + (exponent < 0 ? "-" : "+") + (absolute < 10 ? "0" : "") + absolute;
        }
        String text = decimal.toPlainString();
        return text.contains(".") ? text : text + ".0";
    }

    private static String prefixed(String text, String prefix) {
        if (prefix.isEmpty()) return text;
        String speaker = prefix.replaceFirst("[:：]+$", "");
        return Pattern.compile("^\\s*" + Pattern.quote(speaker) + "\\s*[:：]").matcher(text).find()
                ? text : prefix + text;
    }

    private static ObjectNode message(String role, String content) {
        return JSON.createObjectNode().put("role", role).put("content", content);
    }

    private static String property(Properties properties, String name) {
        return required(properties.getProperty(name), "configuration " + name).trim();
    }

    private static URI endpoint(URI base, String path) {
        return URI.create(base.toString().replaceAll("/+$", "") + path);
    }

    private static void baseUri(URI value) {
        require(value != null && ("https".equals(value.getScheme()) || "http".equals(value.getScheme())) && value.getHost() != null
                && value.getUserInfo() == null && value.getQuery() == null && value.getFragment() == null,
                "service base URL must contain only scheme, host, port and optional path prefix");
    }

    private static String encode(String value) { return URLEncoder.encode(value, StandardCharsets.UTF_8); }

    private static String required(String value, String field) {
        require(value != null && !value.isBlank(), field + " is required");
        return value;
    }

    private static void require(boolean condition, String error) {
        if (!condition) throw new IllegalArgumentException(error);
    }

    private static String sha256(String text) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }
}
