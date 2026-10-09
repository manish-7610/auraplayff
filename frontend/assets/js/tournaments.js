/**
 * AuraPlayFF — Tournaments Module
 * Wraps all tournament-related API calls.
 */
import { apiGet, apiPost } from './api.js';

// ── List tournaments ──────────────────────────────────────────────────────────

/**
 * @param {object} params - { status?, limit?, offset? }
 */
export function listTournaments(params = {}) {
  return apiGet('/api/tournaments', params);
}

// ── Tournament detail ─────────────────────────────────────────────────────────

export function getTournament(tournamentId) {
  return apiGet(`/api/tournaments/${tournamentId}`);
}

// ── Join tournament ───────────────────────────────────────────────────────────

export function joinTournament(tournamentId) {
  return apiPost(`/api/tournaments/${tournamentId}/join`, {});
}

// ── My entry for a tournament ─────────────────────────────────────────────────

export function getMyEntry(tournamentId) {
  return apiGet(`/api/tournaments/${tournamentId}/my-entry`);
}

// ── Room credentials ──────────────────────────────────────────────────────────

export function getRoomCredentials(tournamentId) {
  return apiGet(`/api/tournaments/${tournamentId}/room`);
}

// ── Results ───────────────────────────────────────────────────────────────────

export function getTournamentResults(tournamentId) {
  return apiGet(`/api/tournaments/${tournamentId}/results`);
}

export function getMyResult(tournamentId) {
  return apiGet(`/api/tournaments/${tournamentId}/my-result`);
}

// ── Leaderboard ───────────────────────────────────────────────────────────────

export function getTournamentLeaderboard(params = {}) {
  return apiGet('/api/leaderboard/tournaments', params);
}

export function getPlayerLeaderboard(params = {}) {
  return apiGet('/api/leaderboard/players', params);
}

export function getPlayerPublicStats(username) {
  return apiGet(`/api/leaderboard/players/${encodeURIComponent(username)}`);
}

/**
 * Per-player tournament history with full ranked result sets.
 * @param {string} username
 * @param {object} params - { limit?, offset? }
 * @returns {Promise<{items: Array, total: number, limit: number, offset: number}>}
 */
export function getPlayerTournamentHistory(username, params = {}) {
  return apiGet(`/api/leaderboard/players/${encodeURIComponent(username)}/tournament-history`, params);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

export function isJoinable(tournament) {
  // Players may join when the tournament is OPEN or ROOM_RELEASED (until it goes LIVE).
  return (tournament.status === 'open' || tournament.status === 'room_released')
    && tournament.slots_remaining > 0
    && !tournament.already_joined;
}

export function slotPercentage(tournament) {
  const pct = (tournament.player_count / tournament.max_players) * 100;
  return Math.min(100, Math.round(pct));
}
