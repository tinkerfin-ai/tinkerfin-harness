import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

export interface ErrorFallbackArguments {
  error: Error
  reset: () => void
}

export interface ErrorBoundaryProps {
  children: ReactNode
  fallback: (arguments_: ErrorFallbackArguments) => ReactNode
  resetKey?: string | number
  onError?: (error: Error, info: ErrorInfo) => void
}

interface ErrorBoundaryState {
  error?: Error
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = {}

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.props.onError?.(error, info)
  }

  componentDidUpdate(previousProps: ErrorBoundaryProps) {
    if (
      this.state.error
      && previousProps.resetKey !== this.props.resetKey
    ) {
      this.reset()
    }
  }

  reset = () => {
    this.setState({ error: undefined })
  }

  render() {
    const { error } = this.state
    return error
      ? this.props.fallback({ error, reset: this.reset })
      : this.props.children
  }
}
