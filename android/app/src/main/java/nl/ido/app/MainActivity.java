package nl.ido.app;

import android.app.Activity;
import android.app.KeyguardManager;
import android.content.Intent;
import android.graphics.Color;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.webkit.CookieManager;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;

/**
 * Opens the dashboard of your own server in full screen, behind your fingerprint (or screen lock).
 * Nothing is stored in the app except the login cookie. The server password still exists; the lock protects this phone's app.
 */
public class MainActivity extends Activity {
    private static final long RELOCK_MS = 20_000;   // locks again after 20 seconds in the background

    private WebView web;
    private TextView lock;
    private boolean unlocked = false, asking = false;
    private long pausedAt = 0;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        Window window = getWindow();
        window.setStatusBarColor(Color.parseColor("#0D0B07"));
        window.setNavigationBarColor(Color.parseColor("#0D0B07"));

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0D0B07"));
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0D0B07"));
        lock = new TextView(this);
        lock.setText("🔒  Tik om te ontgrendelen");
        lock.setTextColor(Color.parseColor("#E3B54A"));
        lock.setTextSize(20);
        lock.setGravity(Gravity.CENTER);
        lock.setBackgroundColor(Color.parseColor("#0D0B07"));
        lock.setOnClickListener(v -> askFingerprint());
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        root.addView(lock, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);

        final String home = getString(R.string.app_url);
        final String host = Uri.parse(home).getHost();
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri uri = req.getUrl();
                if ("https".equals(uri.getScheme()) && host.equals(uri.getHost())) return false;
                startActivity(new Intent(Intent.ACTION_VIEW, uri));   // other sites open in the normal browser
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                CookieManager.getInstance().flush();                   // keep the login on this phone
            }
        });
        if (state != null) web.restoreState(state); else web.loadUrl(home);
        setLocked(true);
    }

    private boolean canLock() {
        KeyguardManager km = (KeyguardManager) getSystemService(KEYGUARD_SERVICE);
        return km != null && km.isDeviceSecure();   // without any screen lock there is nothing to ask for
    }

    private void setLocked(boolean on) {
        unlocked = !on;
        lock.setVisibility(on ? View.VISIBLE : View.GONE);
        web.setVisibility(on ? View.INVISIBLE : View.VISIBLE);
    }

    private void askFingerprint() {
        if (asking) return;
        if (!canLock()) { setLocked(false); return; }
        asking = true;
        BiometricPrompt.Builder b = new BiometricPrompt.Builder(this)
                .setTitle(getString(R.string.app_name))
                .setSubtitle("Ontgrendel met je vingerafdruk");
        if (Build.VERSION.SDK_INT >= 30) {
            b.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG
                    | BiometricManager.Authenticators.DEVICE_CREDENTIAL);   // PIN/pattern if the fingerprint fails
        } else {
            b.setNegativeButton("Annuleren", getMainExecutor(), (d, w) -> asking = false);
        }
        b.build().authenticate(new CancellationSignal(), getMainExecutor(), new BiometricPrompt.AuthenticationCallback() {
            @Override
            public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                asking = false;
                setLocked(false);
            }

            @Override
            public void onAuthenticationError(int code, CharSequence message) {
                asking = false;   // stays locked; tap the screen to try again
            }
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (unlocked && pausedAt != 0 && System.currentTimeMillis() - pausedAt > RELOCK_MS) setLocked(true);
        if (!unlocked) askFingerprint();
    }

    @Override
    protected void onPause() {
        super.onPause();
        pausedAt = System.currentTimeMillis();
        CookieManager.getInstance().flush();
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    public void onBackPressed() {
        if (unlocked && web.canGoBack()) web.goBack(); else super.onBackPressed();
    }
}
