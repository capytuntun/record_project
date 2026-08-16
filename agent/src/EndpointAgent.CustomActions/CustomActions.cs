using System;
using WixToolset.Dtf.WindowsInstaller;

namespace EndpointAgent.CustomActions;

/// <summary>
/// MSI custom action that gates uninstall on the administrator password
/// (CLAUDE.md sections 12 and 19).
///
/// Scheduled immediately during an uninstall. It takes the password from the
/// UNINSTALLPWD property when one was supplied on the command line, and
/// otherwise ASKS for it, so that removing the agent from "Apps &amp; Features"
/// works like any other protected application instead of dead-ending. All the
/// decision logic lives in <see cref="UninstallGuard"/>, under unit test; this
/// method is the bridge to Windows Installer and the retry loop.
///
/// A refused attempt is recorded for the agent to report to the management
/// server (<see cref="UninstallAttempts"/>), so an administrator finds out that
/// someone tried.
///
/// NOTE: this runs only during a real (elevated) uninstall, so it is verified by
/// unit-testing UninstallGuard plus a documented on-machine test plan -- not by
/// an automated test in the build environment. It is written to fail OPEN: any
/// error here allows the uninstall.
/// </summary>
public static class CustomActions
{
    /// <summary>
    /// INSTALLUILEVEL: 2 = none (/qn), 3 = basic (/qb), 4 = reduced, 5 = full.
    ///
    /// Prompt at basic (3) and above. This is the fix for "no password prompt
    /// when removing from Apps &amp; Features": Windows 11's Settings &gt; Apps
    /// runs the MSI uninstall at BASIC UI (level 3), not full UI as the classic
    /// Control Panel does -- so a level-4 threshold silently skipped the prompt
    /// on exactly the path most users take. A user initiated the removal and has
    /// a visible desktop at level 3, so a modal dialog is safe there. Only fully
    /// silent runs (/qn, level 2 = no user, no desktop) still skip the prompt and
    /// are refused, because a dialog there would hang the process forever.
    /// </summary>
    private const int MinimumUiLevelToPrompt = 3;

    [CustomAction]
    public static ActionResult CheckUninstallPassword(Session session)
    {
        try
        {
            var configPath = UninstallGuard.DefaultConfigPath;
            var supplied = session["UNINSTALLPWD"] ?? "";

            // Is an uninstall password even configured for this package? If not,
            // the removal proceeds ungated -- but the server should STILL learn
            // someone removed the agent, so an alert fires. Without this, a
            // password-less package (the common "I forgot to set one" case)
            // uninstalls with no prompt AND no alert, which is exactly the gap
            // being closed here.
            var hash = TryReadHash(configPath);
            if (string.IsNullOrEmpty(hash))
            {
                session.Log("EndpointAgent: no uninstall password configured; reporting and allowing.");
                UninstallReporter.TryReport(configPath, "UNPROTECTED");
                return ActionResult.Success;
            }

            var result = UninstallGuard.Evaluate(configPath, supplied);

            // Correct password supplied on the command line: a legitimate,
            // authorised removal -- allow it silently, no alert.
            if (result.Allowed)
            {
                session.Log("EndpointAgent: uninstall password accepted.");
                return ActionResult.Success;
            }

            // A wrong password on the command line is itself an attempt worth
            // recording; the prompt loop records its own.
            if (result.Decision == UninstallGuard.Decision.BlockWrongPassword)
            {
                UninstallAttempts.Record(UninstallAttempts.DefaultPath, "WRONG_PASSWORD");
            }

            // No password on the command line, but one is required: ask, rather
            // than refusing someone who has no way to supply it.
            if (result.Decision == UninstallGuard.Decision.BlockNoPassword && CanPrompt(session))
            {
                var prompted = PromptForPassword(session, configPath);
                if (prompted.Allowed)
                {
                    session.Log("EndpointAgent: uninstall password accepted after prompt.");
                    return ActionResult.Success;
                }
                result = prompted;
            }
            else if (result.Decision == UninstallGuard.Decision.BlockNoPassword)
            {
                UninstallAttempts.Record(UninstallAttempts.DefaultPath, "NO_PASSWORD");
            }

            // Blocked. Report it to the server now (immediate alert), in addition
            // to the on-disk log the still-running agent forwards on its next
            // heartbeat, then refuse the removal.
            var outcome = result.Decision == UninstallGuard.Decision.BlockWrongPassword
                ? "WRONG_PASSWORD" : "NO_PASSWORD";
            UninstallReporter.TryReport(configPath, outcome);

            session.Log("EndpointAgent: uninstall blocked -- " + result.Decision);
            ShowMessage(session, result.Message);
            return ActionResult.Failure;
        }
        catch (Exception ex)
        {
            // Fail OPEN. A bug in the guard must never make the product
            // impossible to remove (section 19).
            try { session.Log("EndpointAgent: uninstall guard error, allowing removal: " + ex.Message); }
            catch (Exception) { /* logging must not change the outcome */ }
            return ActionResult.Success;
        }
    }

    private static string? TryReadHash(string configPath)
    {
        try { return UninstallGuard.ReadAdminPasswordHash(configPath); }
        catch (Exception) { return null; }
    }

    private static bool CanPrompt(Session session)
    {
        try
        {
            return int.TryParse(session["UILevel"], out var level) && level >= MinimumUiLevelToPrompt;
        }
        catch (Exception)
        {
            // Cannot tell what UI we have -> assume none and do not risk hanging.
            return false;
        }
    }

    /// <summary>
    /// Ask for the password until it is right or the user gives up.
    ///
    /// No attempt limit, and the dialog never says how many tries have been made
    /// or remain. Two reasons:
    ///
    ///   * A counter tells whoever is guessing how much room they have left, and
    ///     lets them stop just short of whatever they think the threshold is. It
    ///     is information that only helps the guesser.
    ///   * A lockout after N tries would be a denial-of-service against the
    ///     legitimate administrator who mistyped, on a machine they are standing
    ///     in front of. Cancel is always available, so an unbounded loop traps
    ///     nobody.
    ///
    /// What replaces the limit is reporting: EVERY wrong password is recorded
    /// individually and forwarded to the management server, so guessing gets
    /// noisier the longer it goes on rather than quietly stopping.
    /// </summary>
    private static UninstallGuard.Result PromptForPassword(Session session, string configPath)
    {
        const string first = "此程式由貴公司 IT 管理，移除需要 IT 密碼。\n\n請輸入密碼以繼續移除。";
        const string retry = "密碼錯誤。\n\n請重新輸入 IT 密碼，或按「取消」放棄移除。";

        var message = first;
        var wrong = 0;

        while (true)
        {
            if (PasswordPrompt.Ask(message, out var typed) == PasswordPrompt.Outcome.Cancelled)
            {
                session.Log($"EndpointAgent: uninstall cancelled at password prompt after {wrong} wrong.");
                UninstallAttempts.Record(UninstallAttempts.DefaultPath, "CANCELLED");
                return new UninstallGuard.Result
                {
                    Decision = UninstallGuard.Decision.BlockNoPassword,
                    Message = "已取消移除。",
                };
            }

            var check = UninstallGuard.Evaluate(configPath, typed);
            if (check.Allowed)
            {
                session.Log($"EndpointAgent: uninstall password accepted after {wrong} wrong.");
                return check;
            }

            // Written as it happens, not batched at the end: if this uninstall
            // never finishes, the attempts are already on disk for the agent to
            // report on its next heartbeat.
            wrong++;
            UninstallAttempts.Record(UninstallAttempts.DefaultPath, "WRONG_PASSWORD");
            message = retry;
        }
    }

    private static void ShowMessage(Session session, string text)
    {
        try
        {
            using var record = new Record(0);
            record[0] = text;
            session.Message(InstallMessage.Error | (InstallMessage)MessageButtons.OK, record);
        }
        catch (Exception)
        {
            // If showing the message fails, still block -- but never throw.
        }
    }
}
