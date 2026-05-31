import {
  ButtonItem,
  Navigation,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import {
  addEventListener,
  removeEventListener,
  callable,
  definePlugin,
} from "@decky/api";
import { useEffect, useState } from "react";
import { FaRegDotCircle } from "react-icons/fa";

interface Condition {
  byte: number;
  mask: number;
}

interface Settings {
  enabled: boolean;
  trigger: Condition[];
  label: string;
  device_found: boolean;
  running: boolean;
}

const getSettings = callable<[], Settings>("get_settings");
const setEnabledCall = callable<[boolean], Settings>("set_enabled");
const beginCapture = callable<[], boolean>("begin_capture");
const cancelCapture = callable<[], void>("cancel_capture");
const saveTrigger = callable<[Condition[], string], Settings>("save_trigger");
const clearTrigger = callable<[], Settings>("clear_trigger");

// Open the Quick Access Menu. Navigation.OpenQuickAccessMenu is provided by
// the Steam frontend (re-exported through @decky/ui) and works in-game too,
// because this code runs in the always-loaded SharedJSContext.
function openQAM() {
  try {
    Navigation.OpenQuickAccessMenu();
  } catch (e) {
    console.error("[decky-QAM] failed to open the Quick Access Menu", e);
  }
}

function describe(conditions: Condition[]): string {
  if (!conditions.length) return "Not set";
  const parts = conditions.map((c) => `byte ${c.byte}:0x${c.mask.toString(16)}`);
  return `Back button (${parts.join(", ")})`;
}

function Content() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [capturing, setCapturing] = useState(false);
  const [captureMsg, setCaptureMsg] = useState("");

  const refresh = async () => setSettings(await getSettings());

  useEffect(() => {
    refresh();

    const onCapture = (payload: any) => {
      const phase = payload?.phase;
      if (phase === "press") {
        setCaptureMsg("Now press and hold the back button you want, then release.");
      } else if (phase === "timeout") {
        setCapturing(false);
        setCaptureMsg("No button detected. Try again and hold it firmly.");
      } else if (phase === "done") {
        const conditions: Condition[] = payload.conditions || [];
        saveTrigger(conditions, describe(conditions)).then((s) => {
          setSettings(s);
          setCapturing(false);
          setCaptureMsg("Bound! Press that button any time to open this menu.");
        });
      }
    };

    addEventListener("qam_capture", onCapture);
    return () => removeEventListener("qam_capture", onCapture);
  }, []);

  const startCapture = async () => {
    setCaptureMsg("Hold the controller still for a moment...");
    setCapturing(true);
    const ok = await beginCapture();
    if (!ok) {
      setCapturing(false);
      setCaptureMsg("Could not access the controller. Is this a Steam Deck?");
    }
  };

  const stopCapture = async () => {
    await cancelCapture();
    setCapturing(false);
    setCaptureMsg("");
  };

  if (!settings) {
    return (
      <PanelSection>
        <PanelSectionRow>Loading...</PanelSectionRow>
      </PanelSection>
    );
  }

  return (
    <>
      <PanelSection title="Quick Access Menu remap">
        <PanelSectionRow>
          <ToggleField
            label="Enabled"
            checked={settings.enabled}
            disabled={!settings.trigger.length}
            onChange={async (v) => setSettings(await setEnabledCall(v))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ fontSize: "0.8em", opacity: 0.8 }}>
            Trigger: {settings.label || describe(settings.trigger)}
          </div>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Bind a button">
        {!capturing ? (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={startCapture}>
              {settings.trigger.length ? "Re-bind button" : "Set trigger button"}
            </ButtonItem>
          </PanelSectionRow>
        ) : (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={stopCapture}>
              Cancel
            </ButtonItem>
          </PanelSectionRow>
        )}
        {captureMsg ? (
          <PanelSectionRow>
            <div style={{ fontSize: "0.8em", opacity: 0.9 }}>{captureMsg}</div>
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      <PanelSection title="Tools">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={openQAM}>
            Test: open QAM now
          </ButtonItem>
        </PanelSectionRow>
        {settings.trigger.length ? (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={async () => {
                setSettings(await clearTrigger());
                setCaptureMsg("");
              }}
            >
              Clear binding
            </ButtonItem>
          </PanelSectionRow>
        ) : null}
        {!settings.device_found ? (
          <PanelSectionRow>
            <div style={{ fontSize: "0.8em", color: "#ffae42" }}>
              Steam Deck controller not detected. Button binding will not work.
            </div>
          </PanelSectionRow>
        ) : null}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // Always-on listener so the bound button opens the QAM even when this
  // panel is closed or a game is running.
  const onTrigger = () => openQAM();
  addEventListener("qam_trigger", onTrigger);

  return {
    name: "Decky QAM",
    titleView: <div className={staticClasses.Title}>Decky QAM</div>,
    content: <Content />,
    icon: <FaRegDotCircle />,
    onDismount() {
      removeEventListener("qam_trigger", onTrigger);
    },
  };
});
