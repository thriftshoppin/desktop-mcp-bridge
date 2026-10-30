# Visible test windows for selftest.py. Mode 'button': one button, window title counts clicks. Mode 'password': a password box + a plain box.
param([string]$Mode = 'button', [int]$X = 200, [int]$Y = 200)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class BridgeTestDpi { [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr c); }
"@
[void][BridgeTestDpi]::SetProcessDpiAwarenessContext([IntPtr]-4)   # per-monitor v2: Location/Size are physical pixels
[System.Windows.Forms.Application]::EnableVisualStyles()
$f = New-Object System.Windows.Forms.Form
$f.Text = "BridgeTest $Mode clicks=0"
$f.StartPosition = 'Manual'
$f.Location = New-Object System.Drawing.Point($X, $Y)
$f.Size = New-Object System.Drawing.Size(520, 260)
$f.TopMost = $true
$script:n = 0
if ($Mode -eq 'button') {
    $b = New-Object System.Windows.Forms.Button
    $b.Name = 'TargetButton'; $b.Text = 'Target'
    $b.Location = New-Object System.Drawing.Point(30, 30); $b.Size = New-Object System.Drawing.Size(200, 80)
    $b.Add_Click({ $script:n++; $f.Text = "BridgeTest button clicks=$($script:n)" })
    $f.Controls.Add($b)
} else {
    $pw = New-Object System.Windows.Forms.TextBox
    $pw.Name = 'PwBox'; $pw.UseSystemPasswordChar = $true
    $pw.Location = New-Object System.Drawing.Point(30, 30); $pw.Size = New-Object System.Drawing.Size(300, 30)
    $pl = New-Object System.Windows.Forms.TextBox
    $pl.Name = 'PlainBox'
    $pl.Location = New-Object System.Drawing.Point(30, 90); $pl.Size = New-Object System.Drawing.Size(300, 30)
    $f.Controls.Add($pw); $f.Controls.Add($pl)
    $f.Text = 'BridgeTest password'
}
$f.Add_Shown({ $f.Activate() })
[void]$f.ShowDialog()
