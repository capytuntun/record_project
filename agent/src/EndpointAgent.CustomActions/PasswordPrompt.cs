using System;
using System.Drawing;
using System.Threading;
using System.Windows.Forms;

namespace EndpointAgent.CustomActions;

/// <summary>
/// The password dialog shown when someone removes the agent from
/// "Apps &amp; Features" (CLAUDE.md sections 12 and 19).
///
/// Why a dialog at all: the password can otherwise only arrive on the command
/// line (<c>msiexec /x {code} UNINSTALLPWD=...</c>), which the Settings app's
/// Uninstall button has no way to supply. Without this, a password-protected
/// package simply cannot be removed through the normal Windows path -- the user
/// gets a refusal and no way forward, which is exactly the "hard to remove"
/// behaviour section 19 forbids.
///
/// This is shown from an IMMEDIATE custom action, which runs in the invoking
/// user's interactive session. A deferred action runs in the system context and
/// must never show UI, so this must not be moved there.
/// </summary>
internal static class PasswordPrompt
{
    /// <summary>Result of asking the user for the password.</summary>
    internal enum Outcome
    {
        Entered,
        Cancelled,
    }

    /// <summary>
    /// Show the dialog and return what the user typed.
    /// <paramref name="password"/> is empty when they cancelled.
    ///
    /// The dialog is shown on a dedicated STA thread. A WiX DTF custom action
    /// runs on an MTA thread, where <see cref="Form.ShowDialog()"/> is unreliable
    /// (it can throw or never paint); WinForms requires single-threaded
    /// apartment. Running it here, and joining, keeps the custom action's own
    /// threading model untouched while giving the dialog the apartment it needs.
    /// </summary>
    internal static Outcome Ask(string message, out string password)
    {
        var outcome = Outcome.Cancelled;
        var typed = "";

        var thread = new Thread(() =>
        {
            outcome = ShowDialog(message, out var entered);
            typed = entered;
        })
        {
            IsBackground = true,
        };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();

        password = typed;
        return outcome;
    }

    private static Outcome ShowDialog(string message, out string password)
    {
        password = "";

        using var form = new Form
        {
            Text = "移除端點管理 Agent",
            FormBorderStyle = FormBorderStyle.FixedDialog,
            StartPosition = FormStartPosition.CenterScreen,
            MinimizeBox = false,
            MaximizeBox = false,
            ShowInTaskbar = true,
            ClientSize = new Size(430, 165),
            // msiexec owns the foreground here; without this the dialog can open
            // behind the installer window and look like a hang.
            TopMost = true,
        };

        var label = new Label
        {
            Text = message,
            AutoSize = false,
            Bounds = new Rectangle(16, 14, 398, 58),
        };

        var input = new TextBox
        {
            UseSystemPasswordChar = true,
            Bounds = new Rectangle(16, 78, 398, 24),
        };

        var ok = new Button
        {
            Text = "確定",
            DialogResult = DialogResult.OK,
            Bounds = new Rectangle(238, 118, 85, 28),
        };

        var cancel = new Button
        {
            Text = "取消",
            DialogResult = DialogResult.Cancel,
            Bounds = new Rectangle(329, 118, 85, 28),
        };

        form.Controls.Add(label);
        form.Controls.Add(input);
        form.Controls.Add(ok);
        form.Controls.Add(cancel);
        form.AcceptButton = ok;
        form.CancelButton = cancel;
        form.Shown += (_, _) => { form.Activate(); input.Focus(); };

        var result = form.ShowDialog();
        if (result != DialogResult.OK)
        {
            return Outcome.Cancelled;
        }

        password = input.Text;
        return Outcome.Entered;
    }
}
