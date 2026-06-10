<?php
/**
 * Plugin Name: Sporløs Analytics
 * Plugin URI: https://sporlos.no
 * Description: Cookieløs, samtykke-fri webanalyse fra Sporløs — uten cookie-banner. Legger inn sporings-snippeten på alle offentlige sider.
 * Version: 0.1.0
 * Author: Datamynt AS
 * Author URI: https://datamynt.no
 * License: GPLv2 or later
 * License URI: https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain: sporlos-analytics
 */

if (!defined('ABSPATH')) {
    exit; // Direktekall stoppes.
}

const SPORLOS_DEFAULT_BASE = 'https://sporlos.no';

/** Innstillinger: Innstillinger → Sporløs */
function sporlos_register_settings()
{
    register_setting('sporlos', 'sporlos_site_id', [
        'type' => 'string',
        'sanitize_callback' => 'sporlos_sanitize_site_id',
        'default' => '',
    ]);
    register_setting('sporlos', 'sporlos_api_base', [
        'type' => 'string',
        'sanitize_callback' => 'esc_url_raw',
        'default' => SPORLOS_DEFAULT_BASE,
    ]);
    register_setting('sporlos', 'sporlos_track_logged_in', [
        'type' => 'boolean',
        'sanitize_callback' => 'rest_sanitize_boolean',
        'default' => false,
    ]);
}
add_action('admin_init', 'sporlos_register_settings');

function sporlos_sanitize_site_id($v)
{
    // Site-ID er URL-safe base64-aktig (a-z A-Z 0-9 _ -), maks 32 tegn.
    return substr(preg_replace('/[^A-Za-z0-9_-]/', '', (string) $v), 0, 32);
}

function sporlos_settings_menu()
{
    add_options_page('Sporløs', 'Sporløs', 'manage_options', 'sporlos', 'sporlos_settings_page');
}
add_action('admin_menu', 'sporlos_settings_menu');

function sporlos_settings_page()
{
    if (!current_user_can('manage_options')) {
        return;
    }
    ?>
    <div class="wrap">
      <h1>Sporløs Analytics</h1>
      <p>Cookieløs webanalyse uten cookie-banner. Finn site-ID-en din i
         <a href="https://sporlos.no/app" target="_blank" rel="noopener">Sporløs-dashbordet</a>
         under «Vis sporings-kode».</p>
      <form action="options.php" method="post">
        <?php settings_fields('sporlos'); ?>
        <table class="form-table" role="presentation">
          <tr>
            <th scope="row"><label for="sporlos_site_id">Site-ID</label></th>
            <td><input name="sporlos_site_id" id="sporlos_site_id" type="text" class="regular-text"
                       value="<?php echo esc_attr(get_option('sporlos_site_id', '')); ?>"
                       placeholder="f.eks. 6LIACtOSP-S7"></td>
          </tr>
          <tr>
            <th scope="row"><label for="sporlos_api_base">Sporløs-server</label></th>
            <td><input name="sporlos_api_base" id="sporlos_api_base" type="url" class="regular-text"
                       value="<?php echo esc_attr(get_option('sporlos_api_base', SPORLOS_DEFAULT_BASE)); ?>">
                <p class="description">La stå som standard med mindre du self-hoster Sporløs.</p></td>
          </tr>
          <tr>
            <th scope="row">Innloggede brukere</th>
            <td><label><input name="sporlos_track_logged_in" type="checkbox" value="1"
                       <?php checked(get_option('sporlos_track_logged_in', false)); ?>>
                Mål også innloggede brukere (vanligvis av, så egne redaktører ikke telles)</label></td>
          </tr>
        </table>
        <?php submit_button(); ?>
      </form>
    </div>
    <?php
}

/** Snippet på offentlige sider — enqueued etter WP-reglene, defer-strategi. */
function sporlos_enqueue_script()
{
    $site_id = get_option('sporlos_site_id', '');
    if ($site_id === '') {
        return;
    }
    if (is_user_logged_in() && !get_option('sporlos_track_logged_in', false)) {
        return;
    }
    $base = rtrim(get_option('sporlos_api_base', SPORLOS_DEFAULT_BASE), '/');
    wp_enqueue_script(
        'sporlos-analytics',
        $base . '/sporlos.js',
        array(),
        null, // ekstern, versjonsløs — cache styres av Sporløs-serveren
        array('strategy' => 'defer', 'in_footer' => false)
    );
}
add_action('wp_enqueue_scripts', 'sporlos_enqueue_script');

/** Trackeren leser data-site/data-api fra sin egen script-tag — legg dem på. */
function sporlos_script_attributes($tag, $handle)
{
    if ($handle !== 'sporlos-analytics') {
        return $tag;
    }
    $site_id = get_option('sporlos_site_id', '');
    $base = rtrim(get_option('sporlos_api_base', SPORLOS_DEFAULT_BASE), '/');
    return str_replace(
        ' src=',
        sprintf(' data-site="%s" data-api="%s" src=', esc_attr($site_id), esc_url($base . '/api/event')),
        $tag
    );
}
add_filter('script_loader_tag', 'sporlos_script_attributes', 10, 2);

/** Lenke til innstillinger fra plugin-listen. */
function sporlos_action_links($links)
{
    array_unshift($links, '<a href="' . admin_url('options-general.php?page=sporlos') . '">Innstillinger</a>');
    return $links;
}
add_filter('plugin_action_links_' . plugin_basename(__FILE__), 'sporlos_action_links');
