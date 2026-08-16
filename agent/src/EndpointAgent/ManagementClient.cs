using System.Net;
using System.Net.Http.Json;
using System.Text.Json.Serialization;

namespace EndpointAgent;

public sealed record EnrollResult(
    [property: JsonPropertyName("endpointId")] string EndpointId,
    [property: JsonPropertyName("deviceCredential")] string DeviceCredential,
    [property: JsonPropertyName("credentialExpiresAt")] DateTimeOffset? CredentialExpiresAt,
    [property: JsonPropertyName("heartbeatIntervalSeconds")] int HeartbeatIntervalSeconds);

public sealed record ServerWarning(
    [property: JsonPropertyName("code")] string Code,
    [property: JsonPropertyName("daysRemaining")] int DaysRemaining,
    [property: JsonPropertyName("action")] string? Action,
    [property: JsonPropertyName("message")] string Message);

public sealed record HeartbeatResult(
    [property: JsonPropertyName("endpointId")] string EndpointId,
    [property: JsonPropertyName("credentialDaysRemaining")] int? CredentialDaysRemaining,
    [property: JsonPropertyName("heartbeatIntervalSeconds")] int HeartbeatIntervalSeconds,
    [property: JsonPropertyName("warnings")] List<ServerWarning>? Warnings);

public sealed record RotateResult(
    [property: JsonPropertyName("deviceCredential")] string DeviceCredential,
    [property: JsonPropertyName("credentialExpiresAt")] DateTimeOffset? CredentialExpiresAt);

/// <summary>Server said no, and told us why in terms IT can act on.</summary>
public sealed class EnrollmentRejectedException(string message, string? reason)
    : Exception(message)
{
    public string? Reason { get; } = reason;
}

/// <summary>The credential no longer authenticates -- revoked, expired, or the
/// endpoint was disabled by an administrator.</summary>
public sealed class CredentialRejectedException(string message) : Exception(message);

/// <summary>
/// Talks to the management server. TLS verification is left at the platform
/// default on purpose: there is no option anywhere in this agent to skip
/// certificate validation (CLAUDE.md section 30.9).
/// </summary>
public sealed class ManagementClient : IDisposable
{
    private readonly HttpClient _http;

    public ManagementClient(string serverUrl)
    {
        _http = new HttpClient
        {
            BaseAddress = new Uri(serverUrl.TrimEnd('/') + "/"),
            Timeout = TimeSpan.FromSeconds(30),
        };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("EndpointAgent/0.1.0");
    }

    public async Task<EnrollResult> EnrollAsync(
        string enrollmentToken, DeviceInventory inventory, CancellationToken ct)
    {
        var payload = new
        {
            enrollmentToken,
            deviceName = inventory.DeviceName,
            os = inventory.OsName,
            osVersion = inventory.OsVersion,
            agentVersion = inventory.AgentVersion,
            localUser = inventory.LocalUser,
        };

        using var response = await _http.PostAsJsonAsync("api/agent/enroll", payload, ct);
        if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
        {
            var error = await ReadErrorAsync(response, ct);
            throw new EnrollmentRejectedException(error.Message, error.Reason);
        }
        response.EnsureSuccessStatusCode();

        return await response.Content.ReadFromJsonAsync<EnrollResult>(ct)
            ?? throw new InvalidDataException("Enrollment response was empty.");
    }

    public async Task<HeartbeatResult> HeartbeatAsync(
        string credential, DeviceInventory inventory, ExtendedInventory? extended, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, "api/agent/heartbeat")
        {
            Content = JsonContent.Create(new
            {
                deviceName = inventory.DeviceName,
                os = inventory.OsName,
                osVersion = inventory.OsVersion,
                agentVersion = inventory.AgentVersion,
                localUser = inventory.LocalUser,
                // Null on most heartbeats; the server stores it only when present,
                // so the extended collection runs (and travels) only periodically.
                inventory = extended,
            }),
        };
        request.Headers.Authorization = new("Bearer", credential);

        using var response = await _http.SendAsync(request, ct);
        if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
        {
            var error = await ReadErrorAsync(response, ct);
            throw new CredentialRejectedException(error.Message);
        }
        response.EnsureSuccessStatusCode();

        return await response.Content.ReadFromJsonAsync<HeartbeatResult>(ct)
            ?? throw new InvalidDataException("Heartbeat response was empty.");
    }


    /// <summary>
    /// Forward refused uninstall attempts the installer left behind.
    ///
    /// Separate from the heartbeat on purpose: the heartbeat accepts device
    /// inventory only and is deliberately not audited (one per minute per
    /// endpoint would bury everything else), whereas every one of these is
    /// written to the audit log.
    /// </summary>
    public async Task ReportUninstallAttemptsAsync(
        string credential, IReadOnlyList<UninstallAttempt> attempts, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, "api/agent/uninstall-attempt")
        {
            Content = JsonContent.Create(new { attempts }),
        };
        request.Headers.Authorization = new("Bearer", credential);

        using var response = await _http.SendAsync(request, ct);
        if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
        {
            var error = await ReadErrorAsync(response, ct);
            throw new CredentialRejectedException(error.Message);
        }
        response.EnsureSuccessStatusCode();
    }

    public async Task<RotateResult> RotateCredentialAsync(string credential, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, "api/agent/credential/rotate");
        request.Headers.Authorization = new("Bearer", credential);

        using var response = await _http.SendAsync(request, ct);
        if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
        {
            var error = await ReadErrorAsync(response, ct);
            throw new CredentialRejectedException(error.Message);
        }
        response.EnsureSuccessStatusCode();

        return await response.Content.ReadFromJsonAsync<RotateResult>(ct)
            ?? throw new InvalidDataException("Rotation response was empty.");
    }

    private sealed record ApiError(
        [property: JsonPropertyName("message")] string? Message,
        [property: JsonPropertyName("details")] ApiErrorDetails? Details);

    private sealed record ApiErrorDetails(
        [property: JsonPropertyName("reason")] string? Reason);

    private static async Task<(string Message, string? Reason)> ReadErrorAsync(
        HttpResponseMessage response, CancellationToken ct)
    {
        try
        {
            var error = await response.Content.ReadFromJsonAsync<ApiError>(ct);
            return (error?.Message ?? "伺服器拒絕了這次請求。", error?.Details?.Reason);
        }
        catch (Exception)
        {
            return ($"伺服器回應 {(int)response.StatusCode}。", null);
        }
    }

    public void Dispose() => _http.Dispose();
}
