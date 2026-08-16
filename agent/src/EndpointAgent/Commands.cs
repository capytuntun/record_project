namespace EndpointAgent;

/// <summary>
/// The agent's local command surface.
///
/// Split deliberately along CLAUDE.md sections 12 and 19:
///
///   read-only   -- open to anyone on the machine. A user is entitled to know
///                  their computer is managed, by which server, and how to get
///                  it removed. Hiding that is what a rootkit does.
///   write       -- requires the administrator password set when the package
///                  was generated (section 12).
///   removal     -- documented, never blocked. The password protects the
///                  agent's own settings, not the operating system's ability
///                  to uninstall the product (section 19).
/// </summary>
public static class Commands
{
    public static int Status()
    {
        AgentConfig config;
        try
        {
            config = AgentConfig.Load();
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"無法讀取設定：{ex.Message}");
            return 1;
        }

        var credential = CredentialStore.Load();

        Console.WriteLine("端點管理 Agent");
        Console.WriteLine("─────────────────────────────────────────────");
        Console.WriteLine($"  管理伺服器    {config.ServerUrl}");
        Console.WriteLine($"  組織代號      {config.OrganizationId ?? "（未設定）"}");
        Console.WriteLine($"  端點識別碼    {config.EndpointId ?? "（尚未註冊）"}");
        Console.WriteLine($"  註冊狀態      {(credential is null ? "尚未註冊" : "已註冊")}");
        Console.WriteLine($"  待用註冊憑證  {(string.IsNullOrEmpty(config.EnrollmentToken) ? "無" : "有")}");
        Console.WriteLine($"  心跳間隔      {config.HeartbeatIntervalSeconds} 秒");
        Console.WriteLine($"  設定檔        {AgentConfig.FilePath}");
        Console.WriteLine();
        Console.WriteLine("此電腦由企業 IT 管理。詳情請聯絡貴公司 IT 部門。");
        return 0;
    }

    public static int SetServer(string[] args)
    {
        if (args.Length < 2)
        {
            Console.Error.WriteLine("用法：EndpointAgent set-server <伺服器網址>");
            return 2;
        }

        var url = args[1];
        if (!Uri.TryCreate(url, UriKind.Absolute, out var parsed) || parsed.Scheme != Uri.UriSchemeHttps)
        {
            // Downgrading to http would put the device credential on the wire
            // in clear text (section 24).
            Console.Error.WriteLine("伺服器網址必須是 https://。");
            return 2;
        }

        var config = AgentConfig.Load();
        if (!Authorize(config)) return 3;

        config.ServerUrl = url;
        // A different server means a different identity; force re-enrollment
        // rather than presenting a credential the new server never issued.
        config.EndpointId = null;
        config.Save();
        CredentialStore.Clear();

        Console.WriteLine($"管理伺服器已改為 {url}。重新啟動 EndpointAgent 服務後會重新註冊。");
        return 0;
    }

    public static int ResetEnrollment(string[] args)
    {
        var config = AgentConfig.Load();
        if (!Authorize(config)) return 3;

        CredentialStore.Clear();
        config.EndpointId = null;
        config.EnrollmentToken = args.Length > 1 ? args[1] : config.EnrollmentToken;
        config.Save();

        Console.WriteLine("已清除本機註冊資料。重新啟動 EndpointAgent 服務後會重新註冊。");
        Console.WriteLine("注意：伺服器上原本的端點紀錄仍然存在，請由管理主控台停用或刪除。");
        return 0;
    }

    /// <summary>
    /// Removal instructions, printed to anyone who asks.
    ///
    /// No password. Section 19 forbids building an agent that resists
    /// legitimate maintenance -- withholding the uninstall command would be
    /// exactly that.
    /// </summary>
    public static int UninstallInfo()
    {
        Console.WriteLine("解除安裝端點管理 Agent");
        Console.WriteLine("─────────────────────────────────────────────");
        Console.WriteLine();
        // Whether *this* install is password-gated, rather than describing both
        // cases in the abstract. Whoever runs this is standing at the machine
        // trying to remove it and needs the command that actually works here.
        //
        // The previous version of this help omitted UNINSTALLPWD entirely and
        // stated the agent "never blocks the normal uninstall" -- which is false
        // for a package built with an administrator password, and sends the
        // reader to the Apps & Features button that cannot possibly succeed.
        bool passwordRequired;
        try
        {
            passwordRequired = !string.IsNullOrWhiteSpace(AgentConfig.Load().AdminPasswordHash);
        }
        catch (Exception)
        {
            // Config unreadable: the uninstall guard fails open, so removal is
            // not gated whatever the package was built with.
            passwordRequired = false;
        }

        Console.WriteLine(passwordRequired
            ? "  此安裝需要 IT 密碼才能移除。"
            : "  此安裝不需要密碼即可移除。");
        Console.WriteLine();
        Console.WriteLine("找出產品碼：");
        Console.WriteLine("  Get-ChildItem HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall |");
        Console.WriteLine("    Where-Object { (Get-ItemProperty $_.PSPath).DisplayName -eq");
        Console.WriteLine("      'Endpoint Management Agent' } | Select-Object -ExpandProperty PSChildName");
        Console.WriteLine();
        Console.WriteLine("企業 IT（本機系統管理員權限）：");

        if (passwordRequired)
        {
            Console.WriteLine("  msiexec /x {產品碼} UNINSTALLPWD=<IT 密碼> /qn");
            Console.WriteLine();
            Console.WriteLine("  注意：「設定 → 應用程式」的解除安裝按鈕無法帶入密碼，");
            Console.WriteLine("        對這個安裝一定會失敗。請用上面的指令。");
            Console.WriteLine();
            Console.WriteLine("密碼遺失時（需本機系統管理員權限）：");
            Console.WriteLine($"  1. 更名或刪除 {AgentConfig.FilePath}");
            Console.WriteLine("  2. 再執行 msiexec /x {產品碼} /qn");
            Console.WriteLine("  密碼是防止一般使用者順手移除的門檻，");
            Console.WriteLine("  不是把系統管理員鎖在門外（CLAUDE.md 第 19 節）。");
        }
        else
        {
            Console.WriteLine("  msiexec /x {產品碼} /qn");
            Console.WriteLine("  或從「設定 → 應用程式」移除。");
        }

        Console.WriteLine();
        Console.WriteLine("網域環境：");
        Console.WriteLine("  透過 GPO 軟體派送或 MDM/Intune 收回應用程式。");
        Console.WriteLine();
        Console.WriteLine("若移除卡住或失敗，先停掉服務再試：");
        Console.WriteLine("  sc stop EndpointAgent");
        Console.WriteLine();
        Console.WriteLine("解除安裝後請一併於管理主控台停用該端點，");
        Console.WriteLine("以撤銷其裝置憑證。");
        return 0;
    }

    public static int Help()
    {
        Console.WriteLine("端點管理 Agent");
        Console.WriteLine();
        Console.WriteLine("  status              顯示目前狀態（不需密碼）");
        Console.WriteLine("  uninstall-info      顯示解除安裝方式（不需密碼）");
        Console.WriteLine("  set-server <url>    變更管理伺服器（需要管理密碼）");
        Console.WriteLine("  reset-enrollment    清除註冊資料（需要管理密碼）");
        Console.WriteLine();
        Console.WriteLine("不加參數執行時，以 Windows 服務模式運行。");
        return 0;
    }

    public static int Unknown(string command)
    {
        Console.Error.WriteLine($"未知的指令：{command}");
        Help();
        return 2;
    }

    /// <summary>
    /// Prompts for the administrator password set at package generation.
    /// Returns false and explains itself on failure.
    /// </summary>
    private static bool Authorize(AgentConfig config)
    {
        if (string.IsNullOrWhiteSpace(config.AdminPasswordHash))
        {
            Console.Error.WriteLine(
                "此安裝包未設定管理密碼，因此無法從本機變更設定。請由管理主控台重新產生安裝包。");
            return false;
        }

        Console.Write("管理密碼：");
        var password = ReadPasswordMasked();
        Console.WriteLine();

        if (!AdminPassword.Verify(config.AdminPasswordHash, password))
        {
            Console.Error.WriteLine("密碼錯誤。");
            return false;
        }
        return true;
    }

    private static string ReadPasswordMasked()
    {
        // Console.ReadLine would echo the password onto the screen and into
        // any terminal scrollback the user later shares.
        var buffer = new System.Text.StringBuilder();
        while (true)
        {
            var key = Console.ReadKey(intercept: true);
            if (key.Key == ConsoleKey.Enter) break;
            if (key.Key == ConsoleKey.Backspace)
            {
                if (buffer.Length > 0) buffer.Length--;
                continue;
            }
            if (!char.IsControl(key.KeyChar)) buffer.Append(key.KeyChar);
        }
        return buffer.ToString();
    }
}
