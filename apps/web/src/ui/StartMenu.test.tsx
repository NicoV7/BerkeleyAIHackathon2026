import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useGame } from "../state/store";
import StartMenu from "./StartMenu";

vi.mock("../lib/sfx", () => ({
  sfxMenuClose: vi.fn(),
  sfxMenuHover: vi.fn(),
  sfxMenuOpen: vi.fn(),
  sfxMenuSelect: vi.fn(),
  sfxSubmit: vi.fn(),
}));

const INITIAL = {
  runId: null,
  topic: "",
  playerName: "Player",
  screen: "menu" as const,
  activeEncounterId: null,
  lastYouScores: [],
};

describe("StartMenu info overlays", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useGame.setState({ ...INITIAL });
  });

  it("renders the name flow and informational command buttons", () => {
    render(<StartMenu />);

    expect(screen.getByRole("textbox", { name: /player name/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /start run/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /select avatar logos/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /^about$/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /^controls$/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /^instructions$/i })).toBeVisible();
  });

  it("opens and closes an info overlay", () => {
    render(<StartMenu />);

    fireEvent.click(screen.getByRole("button", { name: /^about$/i }));
    expect(screen.getByRole("dialog", { name: /about/i })).toBeVisible();
    expect(screen.getByText(/monster-catching argument game/i)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes the overlay with Escape", () => {
    render(<StartMenu />);

    fireEvent.click(screen.getByRole("button", { name: /^controls$/i }));
    expect(screen.getByRole("dialog", { name: /controls/i })).toBeVisible();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens the avatar selector and updates the selected avatar", () => {
    render(<StartMenu />);

    fireEvent.click(screen.getByRole("button", { name: /select avatar logos/i }));
    expect(screen.getByRole("dialog", { name: /select avatar/i })).toBeVisible();
    expect(screen.getByText(/evidence tactician/i)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /pathos/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /select avatar pathos/i })).toBeVisible();
  });
});
